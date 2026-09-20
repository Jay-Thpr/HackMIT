"""Local-only tests for fixed ES|QL tools and the explicit-apply setup boundary."""

import importlib.util
import json
from pathlib import Path
import re

import httpx
import pytest

from faultline_telemetry.agent_tools import (
    AgentToolsError,
    AgentToolsRegistry,
    OWNER2_TOOL_IDS,
    TOOLS_ROUTE,
    canonical_kibana_url,
    query_request,
    tool_definitions,
    validate_tool_definition,
)


@pytest.fixture
def setup_script():
    path = Path(__file__).parents[1] / "scripts" / "setup_agent_tools.py"
    spec = importlib.util.spec_from_file_location("setup_agent_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parameters(tool_id=OWNER2_TOOL_IDS[0]):
    values = {
        "incident_id": "incident-1", "environment": "production", "clone_id": "",
        "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:01:00Z",
        "observed_at": "2026-01-01T00:02:00Z", "orders_retry_ratio": 3.0,
        "db_query_p99_ms": 80.0,
    }
    if tool_id == OWNER2_TOOL_IDS[1]:
        values["clone_id"] = "clone-1"
    definition = next(d for d in tool_definitions() if d["id"] == tool_id)
    return {name: values[name] for name in definition["configuration"]["params"]}


def test_exact_registry_and_bounded_named_parameters():
    definitions = tool_definitions()
    assert tuple(d["id"] for d in definitions) == OWNER2_TOOL_IDS
    assert len({d["configuration"]["query"] for d in definitions}) == 4
    for definition in definitions:
        validate_tool_definition(definition)
        assert set(definition) == {"id", "type", "description", "tags", "configuration"}
        assert definition["type"] == "esql"
        config = definition["configuration"]
        query = config["query"]
        assert set(re.findall(r"\?(\w+)", query)) == set(config["params"])
        assert {"incident_id", "environment", "start", "end"} <= set(config["params"])
        assert all(p["optional"] is False for p in config["params"].values())
        assert all(p["type"] in {"string", "date", "float"} for p in config["params"].values())
        assert re.findall(r"FROM ([\w-]+)", query) == [
            "faultline-audit" if definition["id"] == OWNER2_TOOL_IDS[3] else "faultline-fingerprints"
        ]
        assert re.search(r"\| LIMIT (2|20|200)$", query)
        assert "TO_DATETIME(?start) + 90 days" in query
        assert not any(word in query.lower() for word in ("payload", "summary", "controller", "hidden", "benchmark"))
        assert "??" not in query
        assert query_request(definition["id"], parameters(definition["id"]))["query"] == query


def test_distinct_evidence_semantics():
    timeline, comparison, similar, context = tool_definitions()
    assert "incident_id == ?incident_id" in timeline["configuration"]["query"]
    assert "SORT window_start ASC" in timeline["configuration"]["query"]
    query = comparison["configuration"]["query"]
    assert "BY environment, clone_id" in query
    assert 'environment == "clone" AND clone_id == ?clone_id' in query
    assert "AVG(services.orders.retry_ratio)" in query
    assert "COUNT(services.orders.retry_ratio)" in query
    query = similar["configuration"]["query"]
    assert "incident_id != ?incident_id" in query
    assert "clone_id IS NULL" in query
    assert "TO_DATETIME(?end) <= TO_DATETIME(?observed_at)" in query
    assert "services.orders.retry_ratio IS NOT NULL AND db.query_p99_ms IS NOT NULL" in query
    assert "GREATEST(ABS(services.orders.retry_ratio), ABS(?orders_retry_ratio), 1.0)" in query
    assert "retrieval_score DESC" in query and "compared_metrics = 2" in query
    assert "not full fingerprint_similarity" in similar["description"]
    assert "never a causal diagnosis" in similar["description"]
    assert "event_id, stage, kind, actor, action_id, experiment_id" in context["configuration"]["query"]


@pytest.mark.parametrize("change", [
    lambda d: d.update(id="platform.core.search"),
    lambda d: d.update(type="index_search"),
    lambda d: d["configuration"].update(query="FROM * | LIMIT 1"),
    lambda d: d["configuration"]["params"].update(index={"type": "string"}),
    lambda d: d["configuration"]["params"].update({'x | FROM secrets': {"type": "string"}}),
    lambda d: d.update(configuration={"query": "FROM faultline-fingerprints | LIMIT 200", "params": {}}),
])
def test_reject_modified_definitions(change):
    definition = tool_definitions()[0]
    change(definition)
    with pytest.raises(ValueError, match="fixed Owner 2 registry"):
        validate_tool_definition(definition)
    validate_tool_definition(tool_definitions()[0])


def test_quotes_are_bound_values_never_interpolated():
    params = parameters()
    malicious = '\" | FROM secret-index | KEEP * | LIMIT 9999 //'
    params["incident_id"] = malicious
    body = query_request(OWNER2_TOOL_IDS[0], params)
    assert malicious not in body["query"]
    assert body["params"][0] == {"incident_id": malicious}
    params["index"] = "secret-index"
    with pytest.raises(ValueError, match="exact tool parameter keys"):
        query_request(OWNER2_TOOL_IDS[0], params)


@pytest.mark.parametrize("patch", [
    {"environment": "benchmark"}, {"incident_id": ""}, {"clone_id": "clone-1"},
    {"environment": "clone", "clone_id": ""},
    {"start": "bad"}, {"start": "2026-01-01T00:00:00"},
    {"start": "2026-01-01T00:00:00+01:00"},
    {"end": "2025-01-01T00:00:00Z"}, {"end": "2027-01-01T00:00:00Z"},
    {"start": None}, {"incident_id": "x" * 257},
])
def test_invalid_scope_and_time_fail_closed(patch):
    with pytest.raises(ValueError):
        query_request(OWNER2_TOOL_IDS[0], {**parameters(), **patch})


def test_missing_params_unknown_ids_and_clone_scope():
    with pytest.raises(ValueError, match="unknown Owner 2 tool"):
        query_request("platform.core.search", parameters())
    params = parameters()
    del params["end"]
    with pytest.raises(ValueError, match="exact tool parameter keys"):
        query_request(OWNER2_TOOL_IDS[0], params)
    with pytest.raises(ValueError, match="requires clone_id"):
        query_request(OWNER2_TOOL_IDS[1], {**parameters(OWNER2_TOOL_IDS[1]), "clone_id": ""})
    assert query_request(OWNER2_TOOL_IDS[0], {**parameters(), "environment": "clone", "clone_id": "clone-1"})


@pytest.mark.parametrize("patch", [
    {"orders_retry_ratio": None}, {"orders_retry_ratio": float("nan")},
    {"db_query_p99_ms": float("inf")}, {"db_query_p99_ms": -1},
    {"orders_retry_ratio": True}, {"orders_retry_ratio": "3.0"},
    {"environment": "clone"}, {"observed_at": "2025-01-01T00:00:00Z"},
])
def test_similarity_requires_actual_finite_observed_shared_metrics(patch):
    with pytest.raises(ValueError):
        query_request(OWNER2_TOOL_IDS[2], {**parameters(OWNER2_TOOL_IDS[2]), **patch})


@pytest.mark.parametrize("url", [
    "http://primary.example", "https://user:secret@primary.example",
    "https://primary.example?key=secret", "https://primary.example#secret",
    "https://primary.example/api/elsewhere", "https://primary.example/s/../evil",
    "//primary.example", "",
])
def test_reject_noncanonical_url(url):
    with pytest.raises(ValueError):
        canonical_kibana_url(url)


def test_read_only_reconciliation_and_client_close():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    with AgentToolsRegistry("https://primary.example/s/evidence/", "secret", transport=httpx.MockTransport(handler)) as registry:
        plan = registry.reconcile()
        assert [p["operation"] for p in plan] == ["create"] * 4
        assert not registry._client.is_closed
    assert registry._client.is_closed
    assert [(r.method, str(r.url)) for r in requests] == [
        ("GET", "https://primary.example/s/evidence/api/agent_builder/tools")
    ]


def test_exact_create_put_routes_bodies_and_repeat_idempotence():
    definitions = tool_definitions()
    stored = {definitions[0]["id"]: definitions[0], definitions[1]["id"]: {**definitions[1], "description": "old"}}
    stored["unrelated.tool"] = {"id": "unrelated.tool", "type": "index_search"}
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "primary.example"
        assert request.headers["authorization"] == "ApiKey secret"
        assert request.headers["kbn-xsrf"] == "true"
        if request.method == "GET":
            assert request.url.path == TOOLS_ROUTE
            return httpx.Response(200, json={"results": list(stored.values())})
        body = json.loads(request.content)
        if request.method == "POST":
            assert request.url.path == TOOLS_ROUTE
            assert body == next(d for d in definitions if d["id"] == body["id"])
            stored[body["id"]] = body
        else:
            assert request.method == "PUT"
            assert request.url.path == TOOLS_ROUTE + "/" + definitions[1]["id"]
            assert body == {k: definitions[1][k] for k in ("description", "tags", "configuration")}
            stored[definitions[1]["id"]].update(body)
        return httpx.Response(200, json={})

    with AgentToolsRegistry("https://primary.example", "secret", transport=httpx.MockTransport(handler)) as registry:
        assert [p["operation"] for p in registry.reconcile(apply=True)] == ["unchanged", "update", "create", "create"]
        assert [p["operation"] for p in registry.reconcile(apply=True)] == ["unchanged"] * 4
    assert [r.method for r in requests] == ["GET", "PUT", "POST", "POST", "GET"]
    assert stored["unrelated.tool"] == {"id": "unrelated.tool", "type": "index_search"}


@pytest.mark.parametrize("payload", [
    [], {"tools": []}, {"results": {}}, {"results": [None]},
    {"results": [{"id": "x"}, {"id": "x"}]},
    {"results": [{"id": OWNER2_TOOL_IDS[3], "type": "index_search"}]},
    {"results": [{"id": OWNER2_TOOL_IDS[3], "type": "esql", "readonly": True}]},
])
def test_bad_schema_or_fixed_id_collision_never_writes(payload):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    with AgentToolsRegistry("https://primary.example", "secret", transport=httpx.MockTransport(handler)) as registry:
        with pytest.raises(AgentToolsError):
            registry.reconcile(apply=True)
    assert [r.method for r in requests] == ["GET"]


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 409, 429, 500])
def test_http_failures_redacted_and_redirects_not_followed(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="secret remote error", headers={"location": "https://other.example"})

    with AgentToolsRegistry("https://primary.example", "secret", transport=httpx.MockTransport(handler)) as registry:
        with pytest.raises(AgentToolsError, match=f"HTTP {status}") as error:
            registry.reconcile(apply=True)
    assert "secret" not in str(error.value)
    assert len(calls) == 1


def test_transport_failure_redacted():
    def handler(request):
        raise httpx.ConnectError("secret remote details", request=request)

    with AgentToolsRegistry("https://primary.example", "secret", transport=httpx.MockTransport(handler)) as registry:
        with pytest.raises(AgentToolsError, match="transport") as error:
            registry.reconcile()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("argv", [[], ["--dry-run"]])
def test_cli_default_dry_run_needs_no_auth_or_network(setup_script, monkeypatch, capsys, argv):
    def forbidden(*args, **kwargs):
        pytest.fail("offline dry run must not load credentials or connect")

    monkeypatch.setattr(setup_script, "load_repo_dotenv", forbidden)
    monkeypatch.setattr(setup_script, "AgentToolsRegistry", forbidden)
    assert setup_script.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"mode": "dry-run", "tools": tool_definitions()}


@pytest.mark.parametrize("mode", ["--check", "--apply"])
@pytest.mark.parametrize("key_name", ["KIBANA_API_KEY", "ELASTIC_AGENT_BUILDER_API_KEY", "FAULTLINE_ELASTICSEARCH_API_KEY"])
def test_cli_modes_key_fallback_unauthorized_exit_and_cleanup(setup_script, monkeypatch, capsys, mode, key_name):
    for name in ("KIBANA_API_KEY", "ELASTIC_AGENT_BUILDER_API_KEY", "FAULTLINE_ELASTICSEARCH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key_name, "super-secret")
    monkeypatch.setenv("KIBANA_URL", "https://primary.example")
    monkeypatch.setenv("FAULTLINE_SECONDARY_KIBANA_URL", "https://secondary.example")
    monkeypatch.setattr(setup_script, "load_repo_dotenv", lambda path: None)
    clients = []

    def handler(request):
        assert request.url.host == "primary.example"
        assert request.method == "GET"
        assert request.headers["authorization"] == "ApiKey super-secret"
        return httpx.Response(401, text="super-secret")

    def factory(url, key):
        registry = AgentToolsRegistry(url, key, transport=httpx.MockTransport(handler))
        clients.append(registry)
        return registry

    monkeypatch.setattr(setup_script, "AgentToolsRegistry", factory)
    assert setup_script.main([mode]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "HTTP 401" in captured.err
    assert "super-secret" not in captured.err
    assert clients[0]._client.is_closed


def test_cli_invalid_config_and_conflicting_modes(setup_script, monkeypatch, capsys):
    monkeypatch.setattr(setup_script, "load_repo_dotenv", lambda path: None)
    monkeypatch.setenv("KIBANA_URL", "https://user:super-secret@primary.example")
    monkeypatch.setenv("KIBANA_API_KEY", "super-secret")
    assert setup_script.main(["--apply"]) == 2
    captured = capsys.readouterr()
    assert "configuration" in captured.err and "super-secret" not in captured.err
    with pytest.raises(SystemExit) as exit_info:
        setup_script.main(["--apply", "--dry-run"])
    assert exit_info.value.code == 2
