from faultline_telemetry.indices import AUDIT_TEMPLATE, FINGERPRINT_TEMPLATE, ensure_index_templates


class FakeTemplateClient:
    def __init__(self):
        self.puts = []

    def put_index_template(self, name, body):
        self.puts.append((name, body))


def test_ensure_index_templates_puts_both_templates():
    client = FakeTemplateClient()
    ensure_index_templates(client)
    assert client.puts == [
        ("faultline-fingerprints", FINGERPRINT_TEMPLATE),
        ("faultline-audit", AUDIT_TEMPLATE),
    ]


def test_fingerprint_template_maps_dates_and_keywords():
    props = FINGERPRINT_TEMPLATE["template"]["mappings"]["properties"]
    assert FINGERPRINT_TEMPLATE["index_patterns"] == ["faultline-fingerprints*"]
    assert props["window_start"]["type"] == "date"
    assert props["window_end"]["type"] == "date"
    for field in ("incident_id", "clone_id", "environment", "schema_version"):
        assert props[field]["type"] == "keyword"


def test_audit_template_maps_expected_fields():
    props = AUDIT_TEMPLATE["template"]["mappings"]["properties"]
    assert AUDIT_TEMPLATE["index_patterns"] == ["faultline-audit*"]
    assert props["ts"]["type"] == "date"
    assert props["stage"]["type"] == "integer"
    for field in ("incident_id", "clone_id", "environment", "event_id", "kind", "actor", "schema_version"):
        assert props[field]["type"] == "keyword"


def test_numeric_fields_default_to_double():
    templates = FINGERPRINT_TEMPLATE["template"]["mappings"]["dynamic_templates"]
    for name, matched in (("longs_as_double", "long"), ("doubles_as_double", "double")):
        assert any(
            entry.get(name, {}).get("match_mapping_type") == matched
            and entry[name]["mapping"]["type"] == "double"
            for entry in templates
        ), name
