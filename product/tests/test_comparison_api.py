import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from faultline_product.api import create_app


def _recording(recording_id: str = "cmp-alpha") -> dict:
    return {
        "schema_version": "faultline-comparison/1",
        "id": recording_id,
        "title": "Development comparison",
        "created_at": "2026-01-01T00:00:00Z",
        "protocol": {"horizon_s": 120},
        "cases": [{"id": "case-a", "label": "Retry storm", "world": "storm", "expected": "H_meta"}],
        "runs": [
            {"id": "run-1", "case_id": "case-a", "arm": "probe", "source": "live", "status": "completed"},
            {"id": "run-2", "case_id": "case-a", "arm": "elastic", "source": "live", "status": "not_run"},
        ],
    }


def _write(directory: Path, name: str, payload) -> Path:
    path = directory / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def test_missing_directory_lists_nothing(tmp_path):
    client = TestClient(create_app([], comparison_dir=tmp_path / "absent"))
    assert client.get("/api/comparisons").json() == []
    assert client.get("/api/comparisons/cmp-alpha").status_code == 404


def test_list_and_detail_return_saved_recording(tmp_path):
    _write(tmp_path, "cmp-alpha.json", _recording())
    client = TestClient(create_app([], comparison_dir=tmp_path))
    listing = client.get("/api/comparisons").json()
    assert listing == [{"id": "cmp-alpha", "title": "Development comparison", "created_at": "2026-01-01T00:00:00Z", "run_count": 2}]
    detail = client.get("/api/comparisons/cmp-alpha").json()
    assert detail["id"] == "cmp-alpha" and len(detail["runs"]) == 2


def test_malformed_omitted_from_list_and_422_on_detail(tmp_path):
    _write(tmp_path, "cmp-good.json", _recording("cmp-good"))
    _write(tmp_path, "cmp-badjson.json", "{not json")
    _write(tmp_path, "cmp-version.json", {**_recording("cmp-version"), "schema_version": "other/9"})
    _write(tmp_path, "cmp-mismatch.json", _recording("cmp-different"))
    _write(tmp_path, "cmp-noruns.json", {k: v for k, v in _recording("cmp-noruns").items() if k != "runs"})
    _write(tmp_path, "cmp-big.json", " " * (20 * 1024 * 1024 + 1))
    _write(tmp_path, "cmp-nan.json", json.dumps(_recording("cmp-nan")).replace('"horizon_s": 120', '"horizon_s": NaN'))
    _write(tmp_path, "not-a-cmp.json", _recording("not-a-cmp"))
    client = TestClient(create_app([], comparison_dir=tmp_path))
    assert [row["id"] for row in client.get("/api/comparisons").json()] == ["cmp-good"]
    for bad in ["cmp-badjson", "cmp-version", "cmp-mismatch", "cmp-noruns", "cmp-big", "cmp-nan"]:
        assert client.get(f"/api/comparisons/{bad}").status_code == 422, bad
    assert client.get("/api/comparisons/not-a-cmp").status_code == 404


def test_dangling_symlink_does_not_break_listing(tmp_path):
    _write(tmp_path, "cmp-alpha.json", _recording())
    os.symlink(tmp_path / "gone.json", tmp_path / "cmp-dangling.json")
    client = TestClient(create_app([], comparison_dir=tmp_path))
    response = client.get("/api/comparisons")
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == ["cmp-alpha"]


def test_detail_reaches_recordings_beyond_the_100_newest(tmp_path):
    _write(tmp_path, "cmp-old.json", _recording("cmp-old"))
    old = tmp_path / "cmp-old.json"
    stale = time.time() - 10_000
    os.utime(old, (stale, stale))
    for i in range(110):
        _write(tmp_path, f"cmp-new{i:03d}.json", _recording(f"cmp-new{i:03d}"))
    client = TestClient(create_app([], comparison_dir=tmp_path))
    listing = client.get("/api/comparisons").json()
    assert len(listing) == 100
    assert "cmp-old" not in {row["id"] for row in listing}
    assert client.get("/api/comparisons/cmp-old").status_code == 200


def test_symlink_and_outside_files_are_not_served(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = _write(outside, "cmp-secret.json", _recording("cmp-secret"))
    served = tmp_path / "served"
    served.mkdir()
    os.symlink(target, served / "cmp-secret.json")
    _write(served, "cmp-inside.json", _recording("cmp-inside"))
    client = TestClient(create_app([], comparison_dir=served))
    assert [row["id"] for row in client.get("/api/comparisons").json()] == ["cmp-inside"]
    assert client.get("/api/comparisons/cmp-secret").status_code == 404


def test_invalid_ids_are_404_not_traversal(tmp_path):
    _write(tmp_path, "cmp-alpha.json", _recording())
    client = TestClient(create_app([], comparison_dir=tmp_path))
    for bad in ["cmp-..", "x" * 300, "cmp-!bad", "cmp-", "nope"]:
        assert client.get(f"/api/comparisons/{bad}").status_code == 404, bad


def test_routes_are_read_only(tmp_path):
    _write(tmp_path, "cmp-alpha.json", _recording())
    client = TestClient(create_app([], comparison_dir=tmp_path))
    assert client.post("/api/comparisons", json={}).status_code == 405
    assert client.post("/api/comparisons/cmp-alpha", json={}).status_code == 405
