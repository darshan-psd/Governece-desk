"""Regression guard: request cost must not grow with unrelated workspace records.

Runs the same requests against a small and a 40x larger workspace (the extra records belong to
other companies) and checks that SQL statements and response size are identical and that time
does not scale with the data. Full-state endpoints are excluded: their contract is "everything".
"""
import statistics
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.repositories.records import RecordRepository
from tests.perf.benchmark import Api, Probe, build_target, seed

pytestmark = pytest.mark.sqlite_only  # SQLite files, adoption and query plans

SMALL, LARGE = 500, 20000


def profile(n: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        frontend = Path(tmp) / "frontend"
        frontend.mkdir()
        (frontend / "index.html").write_text("<!doctype html>")
        settings = Settings(governance_data_dir=Path(tmp) / "data", governance_frontend_dir=frontend,
                            governance_allowed_hosts="testserver", governance_reminders_enabled=False, _env_file=None)
        app = create_app(settings)
        with TestClient(app, base_url="http://testserver") as client:
            services = app.state.services
            seed(services.db.path, n)
            probe = Probe(services.db.engine)
            api = Api(client)
            api.sign_in()
            target = build_target(api)
            def revision(record_id):
                with services.db.read() as session:
                    return RecordRepository(session, "local-preview").get(record_id)["revision"]

            requests = {
                "edit": lambda: api.req("POST", "/api/v2", json={"action": "edit", "id": target["task"], "revision": revision(target["task"]), "data": {"notes": "x"}}),
                "upload": lambda: api.req("POST", "/api/v2-files", data={"company": target["company"]}, files={"file": ("n.txt", b"n")}),
                "download": lambda: api.req("GET", f"/api/v2-files?id={target['doc']}"),
                "records page": lambda: api.req("GET", f"/api/v2/records?company={target['company']}&limit=20"),
                "audit page": lambda: api.req("GET", "/api/v2/audit?limit=20"),
            }
            results = {}
            for name, call in requests.items():
                times, queries, sizes = [], [], []
                for _ in range(7):
                    before = probe.count
                    started = time.perf_counter()
                    response = call()
                    times.append(time.perf_counter() - started)
                    queries.append(probe.count - before)
                    sizes.append(len(response.content) if name not in ("upload",) else 0)
                results[name] = {"ms": statistics.median(times) * 1000, "queries": statistics.median(queries), "bytes": statistics.median(sizes)}
            api.role(target["client"])
            for name, call in {"client state": lambda: api.req("GET", "/api/v2"), "client portal": lambda: api.req("GET", "/api/portal")}.items():
                times, queries = [], []
                for _ in range(7):
                    before = probe.count
                    started = time.perf_counter()
                    response = call()
                    times.append(time.perf_counter() - started)
                    queries.append(probe.count - before)
                results[name] = {"ms": statistics.median(times) * 1000, "queries": statistics.median(queries), "bytes": len(response.content)}
            return results


@pytest.fixture(scope="module")
def measurements():
    return profile(SMALL), profile(LARGE)


@pytest.mark.parametrize("request_name", ["edit", "upload", "download", "records page", "client state", "client portal"])
def test_cost_does_not_grow_with_unrelated_records(measurements, request_name):
    small, large = measurements[0][request_name], measurements[1][request_name]
    assert large["queries"] == small["queries"], (small, large)
    if request_name not in ("upload", "edit"):
        assert large["bytes"] == small["bytes"], (small, large)
    # 40x more data must not make the request meaningfully slower (generous bound for CI noise).
    assert large["ms"] < max(small["ms"] * 3, small["ms"] + 15), (small, large)


def test_audit_page_is_bounded(measurements):
    small, large = measurements[0]["audit page"], measurements[1]["audit page"]
    assert large["queries"] == small["queries"]
    assert large["ms"] < max(small["ms"] * 3, small["ms"] + 15), (small, large)
