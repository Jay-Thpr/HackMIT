"""Scripted checks of the clone lab (C6) against a running lab manager (:9910) and production stack.

  uv run python scripts/validate_lab.py fairness   # clone starts healthy, carries no hidden state, can't reach :9900
  uv run python scripts/validate_lab.py storm      # db_latency 800ms/20s reproduces the storm; clone C3 retry cap heals it
  uv run python scripts/validate_lab.py degraded   # db_capacity 40 reproduces degraded DB; cap doesn't heal; failover does
  uv run python scripts/validate_lab.py cpu        # cpu_limit payments 0.1: incident that neither cap nor failover heals
  uv run python scripts/validate_lab.py api        # validation errors, ttl auto-revert, undo, reset, destroy, capacity
  uv run python scripts/validate_lab.py all

Uses only the C6 client, the clone's own /stats and C3 control surfaces, and `docker` for the fairness
inspection. Never touches the production fault controller. Exit code 1 if any check failed.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from typing import Any

import httpx

from _common import CONTROL_URL, Sampler, probe
from faultline_contracts.clone import CloneSpec, CloneStatus, HttpCloneLab, LabError, WorkloadSpec
from faultline_contracts.levers import ActionStatus

LAB_URL = "http://127.0.0.1:9910"
PRODUCTION_PROJECT = "faultline-sandbox"
BANNED_SUBSTRINGS = ("io_profile", "fault", "/internal", "/admin", "world", "trigger",
                     "db.statement", "db.query.text", "faultctl")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return ok


def fmt(r: dict[str, Any] | None) -> str:
    if not r:
        return "(no data)"
    keys = ("logical_qps", "retry_ratio", "ok_ratio", "db_issued_qps", "db_p99_ms", "pool_busy_ratio")
    return " ".join(f"{k}={r[k]:.2f}" if isinstance(r[k], float) else f"{k}={r[k]}" for k in keys)


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


class Run:
    def __init__(self) -> None:
        self.lab = HttpCloneLab(LAB_URL, timeout_s=300)
        self.clone = None
        self.s: Sampler | None = None
        self.ctl: httpx.Client | None = None
        self.timeout_ms = 500
        self.project: str | None = None

    # -- clone lifecycle --------------------------------------------------------------------
    def create(self, name: str, **spec: Any) -> bool:
        t0 = time.monotonic()
        self.clone = self.lab.create(CloneSpec(name=name, **spec))
        self.timeout_ms = self.clone.spec.retry_policy.timeout_ms
        self.s = Sampler(urls=self.clone.endpoints.stats_urls)
        self.ctl = httpx.Client(base_url=self.clone.endpoints.control_url, timeout=10)
        return check(f"clone {self.clone.clone_id} ready", self.clone.status == CloneStatus.ready,
                     f"{time.monotonic() - t0:.1f}s, project ports {self.clone.endpoints.control_url}")

    def destroy(self) -> None:
        if self.clone:
            info = self.lab.destroy(self.clone.clone_id)
            check("destroy", info.status == CloneStatus.destroyed and info.endpoints is None)
            self.clone = None

    def reset(self) -> bool:
        t0 = time.monotonic()
        try:
            info = self.lab.reset(self.clone.clone_id)
        except httpx.HTTPError as e:
            return check("clone reset returns healthy", False, str(e))
        return check("clone reset returns healthy", info.status == CloneStatus.ready, f"{time.monotonic() - t0:.1f}s")

    # -- measurement ------------------------------------------------------------------------
    def phase(self, seconds: float, label: str) -> tuple[int, int]:
        i0 = len(self.s.hist)
        self.s.run_for(seconds, label)
        return i0, len(self.s.hist) - 1

    def windows(self, span: tuple[int, int], step: int = 5) -> list[dict[str, Any]]:
        i0, i1 = span
        h = self.s.hist
        return [probe.row(h[i][1], h[min(i + step, i1)][1]) for i in range(max(i0 - 1, 0), i1, step)
                if min(i + step, i1) > i]

    def tail(self, span: tuple[int, int], seconds: int) -> dict[str, Any]:
        i0, i1 = span
        return probe.row(self.s.hist[max(i0, i1 - seconds)][1], self.s.hist[i1][1])

    def healthy(self, r: dict[str, Any]) -> bool:
        return probe.is_healthy(r, self.timeout_ms)

    def baseline(self, seconds: int = 15) -> bool:
        span = self.phase(seconds, "baseline")
        return check("clone baseline healthy", self.healthy(self.tail(span, seconds - 2)), fmt(self.tail(span, seconds - 2)))

    def lever(self, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return self.ctl.post(f"/admin/{path}", json=body) if body is not None else self.ctl.delete(f"/admin/{path}")

    def incident_windows(self, span: tuple[int, int], amplified: bool = True) -> tuple[int, int]:
        ws = self.windows(span)
        bad = [w for w in ws if probe.is_incident(w) and (not amplified or (w["retry_ratio"] or 0) > 2.0)]
        return len(bad), len(ws)

    # -- scenarios ----------------------------------------------------------------------------
    def scenario_fairness(self) -> None:
        print("\n=== fairness: a clone is clean, hidden-state free and cut off from the fault controller")
        self.create("fairness")
        # which compose project publishes the clone's orders port? (the manager's slot scheme is not assumed)
        port = self.clone.endpoints.stats_urls["orders"].rsplit(":", 1)[1]
        project = None
        for cid in docker("ps", "-q", "--filter", "label=com.docker.compose.service=orders").split():
            if f":{port}->" in docker("ps", "--filter", f"id={cid}", "--format", "{{.Ports}}"):
                project = json.loads(docker("inspect", "-f", "{{json .Config.Labels}}", cid))["com.docker.compose.project"]
        check("clone runs in its own compose project", bool(project) and project != PRODUCTION_PROJECT, str(project))
        self.project = project
        names = docker("ps", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.Label \"com.docker.compose.service\"}}").split()
        check("clone has no fault controller container", "faultctl" not in names, ",".join(sorted(names)))
        prod_faultctl_ip = docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                                  f"{PRODUCTION_PROJECT}-faultctl-1").strip()
        code = docker("exec", f"{project}-orders-1", "python", "-c",
                      "import socket,urllib.request,urllib.error\n"
                      "for h in ('faultctl','%s-faultctl-1'):\n"
                      "  try: socket.gethostbyname(h); print('resolves', h)\n"
                      "  except OSError: pass\n"
                      "try: print('answered', urllib.request.urlopen('http://%s:8000/fault/state', timeout=2).status)\n"
                      "except urllib.error.HTTPError as e: print('refused', e.code)\n"
                      "except OSError as e: print('unreachable', type(e).__name__)\n" % (PRODUCTION_PROJECT, prod_faultctl_ip))
        check("production fault controller unusable from inside the clone",
              ("unreachable" in code or "refused 403" in code) and "resolves" not in code, code.strip())
        cost = docker("exec", f"{project}-db-primary-1", "psql", "-U", "app", "-d", "shop", "-tAc",
                      "SELECT base_ms, extra_ms FROM io_profile WHERE id = 1").strip()
        check("clone DB carries no injected cost", cost.endswith("|0"), f"io_profile={cost}")
        rows = docker("exec", f"{project}-db-primary-1", "psql", "-U", "app", "-d", "shop", "-tAc",
                      "SELECT count(*) FROM payments WHERE created_at < now() - interval '5 minutes'").strip()
        check("clone DB has no production history", rows == "0", f"{rows} old rows")
        self.baseline()
        # OTel: read the structured OTLP records the clone collector tees to /tmp/otel/records.jsonl
        # (contrib image has no shell, so docker cp streams the file as a tar on stdout).
        records = self._collector_records(f"{project}-otel-collector-1")
        services, envs, strings = set(), set(), set()
        for rec in records:
            for group in rec.get("resourceSpans") or []:
                services.update(v for k, v in self._attrs(group.get("resource") or {}) if k == "service.name")
            for res in self._resources(rec):
                for k, v in self._attrs(res):
                    if k == "deployment.environment":
                        envs.add(v)
            strings |= self._record_strings(rec)
        check("collector emitted spans from orders and payments",
              {"orders", "payments"} <= services, f"services={sorted(services)}")
        log_count = sum(len(scope.get("logRecords") or []) for rec in records
                        for resource in rec.get("resourceLogs") or []
                        for scope in resource.get("scopeLogs") or [])
        check("collector emitted application log records", log_count > 0, f"records={log_count}")
        banned = BANNED_SUBSTRINGS
        leaked = sorted(s for s in strings if any(b in s.lower() for b in banned))
        check("emitted telemetry carries no hidden state", not leaked, f"leaked={leaked[:10]}")
        check("telemetry tagged deployment.environment=clone-<slot>",
              envs == {f"clone-{project.rsplit('-', 1)[1]}"}, str(envs))
        self._elastic_check(f"clone-{project.rsplit('-', 1)[1]}")
        self.destroy()

    # -- OTel record inspection (clone collector file tee) ------------------------------------
    @staticmethod
    def _collector_records(container: str, path: str = "/tmp/otel/records.jsonl",
                           timeout_s: float = 15) -> list[Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            cp = subprocess.run(["docker", "cp", f"{container}:{path}", "-"], capture_output=True)
            lines: list[Any] = []
            if cp.returncode == 0:
                with tarfile.open(fileobj=io.BytesIO(cp.stdout)) as tf:
                    for m in tf.getmembers():
                        f = tf.extractfile(m)
                        if f:
                            lines = [json.loads(l) for l in f.read().decode().splitlines() if l.strip()]
            if lines or time.monotonic() > deadline:
                return lines
            time.sleep(2)

    @staticmethod
    def _attrs(node: dict[str, Any]):
        for a in node.get("attributes") or []:
            yield a.get("key"), (a.get("value") or {}).get("stringValue")

    @staticmethod
    def _resources(rec: dict[str, Any]):
        for group in ("resourceSpans", "resourceMetrics", "resourceLogs"):
            for rs in rec.get(group) or []:
                yield rs.get("resource") or {}

    @staticmethod
    def _record_strings(rec: dict[str, Any]) -> set[str]:
        """Every attribute key, string attribute value, span name and metric name in the record."""
        out: set[str] = set()

        def attrs(node: dict[str, Any] | None) -> None:
            for a in (node or {}).get("attributes") or []:
                if a.get("key"):
                    out.add(a["key"])
                v = (a.get("value") or {}).get("stringValue")
                if v:
                    out.add(v)

        for rs in rec.get("resourceSpans") or []:
            attrs(rs.get("resource"))
            for ss in rs.get("scopeSpans") or []:
                attrs(ss.get("scope"))
                for sp in ss.get("spans") or []:
                    if sp.get("name"):
                        out.add(sp["name"])
                    attrs(sp)
                    for ev in sp.get("events") or []:
                        attrs(ev)
        for rm in rec.get("resourceMetrics") or []:
            attrs(rm.get("resource"))
            for sm in rm.get("scopeMetrics") or []:
                attrs(sm.get("scope"))
                for met in sm.get("metrics") or []:
                    if met.get("name"):
                        out.add(met["name"])
                    for kind in ("sum", "gauge", "histogram", "exponentialHistogram", "summary"):
                        for dp in (met.get(kind) or {}).get("dataPoints") or []:
                            attrs(dp)
        def log_strings(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    out.add(key)
                    log_strings(value)
            elif isinstance(node, list):
                for value in node:
                    log_strings(value)
            elif isinstance(node, str):
                out.add(node)

        log_strings(rec.get("resourceLogs") or [])
        return out

    # -- optional Elasticsearch cross-check -----------------------------------------------------
    def _elastic_check(self, env: str) -> None:
        for line in self._root_env():
            os.environ.setdefault(*line)
        managed_otlp = bool(os.environ.get("FAULTLINE_OTLP_ENDPOINT"))
        prefix = "FAULTLINE_OBSERVABILITY_ELASTICSEARCH" if managed_otlp else "FAULTLINE_ELASTICSEARCH"
        url, key = os.environ.get(f"{prefix}_URL"), os.environ.get(f"{prefix}_API_KEY")
        url = url.rstrip("/") if url else url
        if not (url and key):
            print(f"SKIP  elastic indexing check: {prefix}_URL/API_KEY not set; tee is local evidence only", flush=True)
            return
        # Verified against the otel-mode docs: resource.attributes.* are keywords; @timestamp is
        # date_nanos. Ingest lag is on the order of seconds — poll up to 60s.
        body = {"size": 200,
                "query": {"bool": {"filter": [
                    {"term": {"resource.attributes.deployment.environment": env}},
                    {"range": {"@timestamp": {"gte": self.clone.created_at.isoformat()}}}]}},
                "aggs": {"svc": {"terms": {"field": "resource.attributes.service.name"}}}}
        deadline = time.monotonic() + 60
        detail = ""
        while True:
            try:
                r = httpx.post(f"{url}/traces-*/_search", json=body,
                               headers={"Authorization": f"ApiKey {key}"}, timeout=15)
                r.raise_for_status()
                data = r.json()
                total = data["hits"]["total"]
                total = total["value"] if isinstance(total, dict) else total
                svcs = {b["key"] for b in data.get("aggregations", {}).get("svc", {}).get("buckets", [])}
                if total > 0 and {"orders", "payments"} <= svcs:
                    check("elastic received clone traces (orders+payments)", True, f"total={total} svcs={sorted(svcs)}")
                    strings = set()
                    for hit in data["hits"]["hits"]:
                        strings |= self._elastic_strings(hit.get("_source") or {})
                    leaked = sorted(s for s in strings if any(b in s.lower() for b in BANNED_SUBSTRINGS))
                    check("elastic traces carry no hidden state", not leaked, f"leaked={leaked[:10]}")
                    return
                detail = f"total={total} svcs={sorted(svcs)}"
            except httpx.HTTPStatusError as e:
                detail = f"HTTP {e.response.status_code}: {e.response.text[:120]}"
            except httpx.HTTPError as e:
                detail = str(e)[:120]
            if time.monotonic() > deadline:
                return check("elastic received clone traces (orders+payments)", False, detail)
            time.sleep(3)

    @staticmethod
    def _elastic_strings(src: dict[str, Any]) -> set[str]:
        """Every attribute key, string value and span name in an otel-mode ES document."""
        out: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    out.add(k)
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
            elif isinstance(node, str):
                out.add(node)

        for container in (src.get("attributes"),
                          (src.get("resource") or {}).get("attributes"),
                          (src.get("scope") or {}).get("attributes")):
            walk(container)
        for ev in src.get("events") or []:
            walk((ev or {}).get("attributes"))
        if src.get("name"):
            out.add(src["name"])
        return out

    @staticmethod
    def _root_env() -> list[tuple[str, str]]:
        path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
        out = []
        try:
            for line in open(path):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    out.append(tuple(line.split("=", 1)))
        except OSError:
            pass
        return out

    def scenario_storm(self, delay_ms: int = 800, duration_s: int = 20, persist_s: int = 45, cap_s: int = 20,
                       after_s: int = 40) -> None:
        print(f"\n=== storm reproduction: db_latency {delay_ms}ms x {duration_s}s, watch {persist_s}s, cap {cap_s}s")
        self.create("h_meta")
        self.baseline()
        h = self.lab.apply(self.clone.clone_id, "db_latency", {"extra_ms": delay_ms}, ttl_s=duration_s)
        self.phase(duration_s + 1, "db_latency active")
        h2 = next(a for a in self.lab.actions(self.clone.clone_id) if a.action_id == h.action_id)
        check("db_latency expired on its own", h2.status == ActionStatus.expired, h2.status.value)
        post = self.phase(persist_s, "slowdown gone")
        n, m = self.incident_windows(post)
        check(f"storm persists {persist_s}s after the slowdown ends (reproduces World A fingerprint)", n == m,
              f"{n}/{m} windows unhealthy+amplified; {fmt(self.tail(post, persist_s))}")
        r = self.lever("retry_override", {"max_retries": 0, "ttl_s": cap_s})
        check("clone C3 retry_cap accepted", r.status_code == 200, r.text[:100])
        cap = self.phase(cap_s, "retry cap 0")
        check("retry cap heals the storm in the clone", self.healthy(self.tail(cap, 10)), fmt(self.tail(cap, 10)))
        after = self.phase(after_s, "cap released")
        ws = self.windows(after)
        good = [w for w in ws if self.healthy(w)]
        check(f"stays healed {after_s}s after the cap (World A)", len(good) == len(ws),
              f"{len(good)}/{len(ws)} healthy; {fmt(self.tail(after, after_s))}")
        self.destroy()

    def scenario_degraded(self, capacity: int = 40, develop_s: int = 30, cap_s: int = 20, after_s: int = 30,
                          failover_s: int = 30) -> None:
        print(f"\n=== degraded-DB reproduction: db_capacity {capacity}; cap doesn't heal; failover does")
        self.create("h_db")
        self.baseline()
        h = self.lab.apply(self.clone.clone_id, "db_capacity", {"capacity_qps": capacity}, ttl_s=600)
        dev = self.phase(develop_s, f"db_capacity {capacity}")
        n, m = self.incident_windows((dev[0] + 10, dev[1]))
        check("degraded DB reproduces the amplified incident (World B fingerprint)", n == m,
              f"{n}/{m}; {fmt(self.tail(dev, 15))}")
        self.lever("retry_override", {"max_retries": 0, "ttl_s": cap_s})
        cap = self.phase(cap_s, "retry cap 0")
        t = self.tail(cap, 10)
        check("cap removes amplification but does not heal", (t["retry_ratio"] or 9) < 1.1 and probe.is_incident(t), fmt(t))
        after = self.phase(after_s, "cap released")
        n, m = self.incident_windows((after[0] + 5, after[1]))
        check("incident returns after the cap (World B)", n == m, f"{n}/{m}; {fmt(self.tail(after, 15))}")
        self.lever("db/failover", {"ttl_s": 300})
        fo = self.phase(failover_s, "failover")
        check("failover heals the degraded clone", self.healthy(self.tail(fo, 10)), fmt(self.tail(fo, 10)))
        self.lab.undo(h)
        self.lever("db/failover")
        self.reset()
        self.baseline()
        self.destroy()

    def scenario_cpu(self, cpus: float = 0.1, develop_s: int = 30, cap_s: int = 20, failover_s: int = 25) -> None:
        print(f"\n=== cpu_limit payments {cpus}: neither cap nor failover heals (none-of-the-above)")
        self.create("h_cpu")
        self.baseline()
        h = self.lab.apply(self.clone.clone_id, "cpu_limit", {"service": "payments", "cpus": cpus}, ttl_s=600)
        dev = self.phase(develop_s, f"cpu {cpus}")
        check("cpu starvation is an incident", probe.is_incident(self.tail(dev, 10)), fmt(self.tail(dev, 10)))
        self.lever("retry_override", {"max_retries": 0, "ttl_s": cap_s})
        cap = self.phase(cap_s, "retry cap 0")
        check("cap does not heal cpu starvation", not self.healthy(self.tail(cap, 10)), fmt(self.tail(cap, 10)))
        self.lever("db/failover", {"ttl_s": 120})
        fo = self.phase(failover_s, "failover")
        check("failover does not heal cpu starvation", not self.healthy(self.tail(fo, 10)), fmt(self.tail(fo, 10)))
        self.lever("db/failover")
        self.lab.undo(h)
        cid = docker("ps", "-q", "--filter", "label=com.docker.compose.service=payments",
                     "--filter", f"publish={self.clone.endpoints.stats_urls['payments'].rsplit(':', 1)[1]}").strip()
        cpu = docker("inspect", "-f", "{{.HostConfig.NanoCpus}}", cid).strip()
        check("undo restores the original cpu limit", cpu == str(int(2e9)), f"NanoCpus={cpu}")
        self.reset()
        self.destroy()

    def scenario_api(self) -> None:
        print("\n=== lab API: validation, ttl, undo, workload, reset, capacity")
        cat = self.lab.catalog()
        check("catalog has 6 actions", {a.id for a in cat} == {"retry_policy", "db_latency", "db_capacity", "cpu_limit",
                                                                 "service_kill", "service_restart"})
        self.create("api")
        cid = self.clone.clone_id
        for name, action, params, ttl in [
            ("bad ttl", "db_latency", {"extra_ms": 10}, 100000),
            ("missing param", "db_latency", {}, 10),
            ("unknown param", "db_latency", {"extra_ms": 10, "x": 1}, 10),
            ("out of range", "cpu_limit", {"service": "payments", "cpus": 0.001}, 10),
            ("bad enum", "service_kill", {"service": "envoy"}, 10),
            ("no v2", "cpu_limit", {"service": "orders-v2", "cpus": 1}, 10),
        ]:
            try:
                self.lab.apply(cid, action, params, ttl_s=ttl)
                check(f"rejects {name}", False, "accepted")
            except LabError as e:
                check(f"rejects {name}", "-> 400" in str(e) or "-> 409" in str(e), str(e)[:90])
        h = self.lab.apply(cid, "retry_policy", {"max_retries": 0, "timeout_ms": 300}, ttl_s=4)
        g = httpx.get(f"{self.clone.endpoints.stats_urls['orders']}/stats").json()["gauges"]
        check("retry_policy applied to orders", g["max_retries"] == 0 and g["attempt_timeout_ms"] == 300, str(g))
        time.sleep(5)
        g = httpx.get(f"{self.clone.endpoints.stats_urls['orders']}/stats").json()["gauges"]
        st = next(a for a in self.lab.actions(cid) if a.action_id == h.action_id).status
        check("retry_policy ttl reverted", g["max_retries"] == 3 and g["attempt_timeout_ms"] == 500 and st == ActionStatus.expired,
              f"{g['max_retries']} {g['attempt_timeout_ms']} {st.value}")
        h = self.lab.apply(cid, "db_latency", {"extra_ms": 5}, ttl_s=60)
        h = self.lab.undo(h)
        check("undo returns undone", h.status == ActionStatus.undone)
        check("undo is idempotent", self.lab.undo(h).status == ActionStatus.undone)
        info = self.lab.set_workload(cid, WorkloadSpec(rps=40))
        time.sleep(4)
        g = httpx.get(f"{self.clone.endpoints.stats_urls['loadgen']}/stats").json()["gauges"]
        check("workload change reaches loadgen", info.spec.workload.rps == 40 and g["target_rps"] == 40, str(g["target_rps"]))
        k = self.lab.apply(cid, "service_kill", {"service": "payments"}, ttl_s=5)
        time.sleep(2)
        try:
            httpx.get(f"{self.clone.endpoints.stats_urls['payments']}/stats", timeout=2).raise_for_status()
            check("service_kill stops payments", False, "still answering")
        except httpx.HTTPError:
            check("service_kill stops payments", True)
        time.sleep(8)
        st = next(a for a in self.lab.actions(cid) if a.action_id == k.action_id).status
        ok = httpx.get(f"{self.clone.endpoints.stats_urls['payments']}/stats", timeout=5).status_code == 200
        check("service_kill ttl restarts payments", ok and st == ActionStatus.expired, st.value)
        r = self.lab.apply(cid, "service_restart", {"service": "orders"}, ttl_s=1)
        check("service_restart is one-shot", r.status == ActionStatus.expired, r.status.value)
        self.reset()
        info = self.lab.get(cid)
        rps = httpx.get(f"{self.clone.endpoints.stats_urls['loadgen']}/stats").json()["gauges"]["target_rps"]
        check("reset clears actions and keeps the (updated) spec workload",
              info.spec.workload.rps == 40 and rps == 40 and not info.active_actions, f"rps={rps}")
        others = [self.lab.create(CloneSpec(name=f"cap{i}")) for i in range(2)]
        try:
            self.lab.create(CloneSpec(name="toomany"))
            check("refuses a 4th clone", False, "accepted")
        except LabError as e:
            check("refuses a 4th clone", "-> 409" in str(e), str(e)[:80])
        ports = {c.endpoints.control_url for c in [self.clone, *others]}
        check("clones get distinct ports", len(ports) == 3, ",".join(sorted(ports)))
        for c in others:
            self.lab.destroy(c.clone_id)
        self.destroy()
        check("destroy is idempotent", self.lab.destroy(cid).status == CloneStatus.destroyed)
        try:
            self.lab.apply(cid, "db_latency", {"extra_ms": 1}, ttl_s=1)
            check("destroyed clone refuses actions", False)
        except LabError as e:
            check("destroyed clone refuses actions", "-> 409" in str(e))
        prod = httpx.get(f"{CONTROL_URL}/admin/levers").json()
        check("production levers untouched", all(not v["active"] for v in prod.values()), str({k: v["active"] for k, v in prod.items()}))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=["fairness", "storm", "degraded", "cpu", "api", "all"])
    a = ap.parse_args()
    run = Run()
    try:
        for name in (["fairness", "api", "storm", "degraded", "cpu"] if a.scenario == "all" else [a.scenario]):
            getattr(run, f"scenario_{name}")()
    finally:
        for c in run.lab.list():
            if c.status not in (CloneStatus.destroyed,):
                run.lab.destroy(c.clone_id)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
