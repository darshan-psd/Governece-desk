"""Scalability benchmark: request cost as unrelated workspace records grow.

Seeds a workspace with N records spread over many companies (realistic mix of kinds), plus one
small "target" company that the measured requests act on. Then measures, per request:
wall time (median), SQL statements executed, response bytes and peak Python memory.

    .venv/bin/python -m tests.perf.benchmark                  # 1k, 10k, 50k, 100k
    .venv/bin/python -m tests.perf.benchmark 1000 10000       # custom sizes
    .venv/bin/python -m tests.perf.benchmark --json out.json  # also write raw results
"""
from __future__ import annotations

import json
import random
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import event

from app.core.config import Settings
from app.main import create_app
from app.repositories.records import RecordRepository

WORKSPACE = "local-preview"
COOKIE = "gov_preview"
# Mix of record kinds per 100 filler records (roughly what a busy secretarial practice accumulates).
MIX = {"company": 1, "task": 38, "document": 15, "notification": 10, "meeting": 5, "request": 5, "invoice": 5,
       "payment": 5, "portalobligation": 5, "thread": 5, "close": 3, "audit-only": 3}


class Probe:
    """Counts SQL statements issued by the app's engine."""

    def __init__(self, engine):
        self.count = 0
        event.listen(engine, "before_cursor_execute", self._hit)

    def _hit(self, *_args):
        self.count += 1


def filler(kind: str, i: int, company: str) -> dict:
    base = {"company": company, "title": f"{kind.title()} {i}", "due": f"2027-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
            "status": random.choice(["Not started", "In progress", "Under review", "Completed"]), "assignee": "owner",
            "priority": "Normal", "notes": "Routine secretarial work item. " * 4, "preparer": "owner", "evidence": [],
            "created": "2026-01-01T00:00:00.000Z"}
    if kind == "document":
        return {"category": "Other", "name": f"file-{i}.pdf", "company": company, "key": f"{WORKSPACE}/{i}", "size": 2048,
                "hash": "ab" * 32, "version": 1, "previous": None, "shared": i % 2 == 0, "uploadedBy": "owner", "scanStatus": "Not scanned"}
    if kind == "company":
        return {"name": f"Company {i}", "jurisdiction": "India", "status": "Not started", "evidence": []}
    if kind == "invoice":
        return {**base, "status": "Issued", "number": f"INV-{i}", "total": 100000, "items": [], "seller": {}, "buyer": {}}
    if kind == "payment":
        return {"company": company, "invoice": f"inv-{i}", "amount": 1000, "reference": f"REF-{i}", "status": "Confirmed"}
    if kind == "thread":
        return {"company": company, "title": "Query", "status": "Open", "messages": [{"by": "owner", "at": "2026-01-01T00:00:00.000Z", "content": "Hello"}], "readBy": {}}
    return base


def seed(db_path: Path, n: int) -> None:
    import sqlite3

    random.seed(7)
    conn = sqlite3.connect(db_path)
    rows, audit = [], []
    kinds = [k for k, weight in MIX.items() for _ in range(weight)]
    companies = max(1, n // 100)
    for i in range(n):
        kind = kinds[i % len(kinds)]
        block = i // len(kinds)  # each block of ~100 records belongs to one company
        company = f"co-{block % companies}"
        if kind == "audit-only":
            audit.append((f"a-{i}", WORKSPACE, "Created task · Workspace owner", f"Task {i}", f"2026-01-01T00:00:{i % 60:02d}.000Z"))
            continue
        record_id = company if kind == "company" else f"{kind}-{i}"
        rows.append((WORKSPACE, record_id, kind, json.dumps(filler(kind, i, company)), 1, f"2026-01-{(i % 28) + 1:02d}T00:00:00.000Z"))
    conn.executemany("INSERT OR IGNORE INTO records (owner,id,kind,data,revision,updated) VALUES (?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO audit (id,owner,action,subject,at) VALUES (?,?,?,?,?)", audit)
    conn.commit()
    conn.close()


class Api:
    def __init__(self, client: TestClient):
        self.client = client
        self.cookie = None

    def sign_in(self):
        self.client.cookies.clear()
        self.cookie = self.client.get("/").cookies.get(COOKIE)

    def role(self, actor: str):
        self.cookie = self.req("POST", "/api/session", json={"actor": actor}).cookies.get(COOKIE)

    def req(self, method: str, path: str, **kw):
        self.client.cookies.clear()
        headers = {"cookie": f"{COOKIE}={self.cookie}"} if self.cookie else {}
        response = self.client.request(method, path, headers=headers, **kw)
        self.client.cookies.clear()
        assert response.status_code < 400, f"{method} {path}: {response.status_code} {response.text[:200]}"
        return response


def build_target(api: Api) -> dict:
    """A small company with real workflow records, created through the API like the React app would."""
    api.req("POST", "/api/v2", json={"action": "initialise"})
    company = api.req("POST", "/api/v2", json={"action": "create", "kind": "company", "data": {"name": "Target Co"}}).json()["id"]
    doc = api.req("POST", "/api/v2-files", data={"company": company, "shared": "true"}, files={"file": ("evidence.txt", b"evidence")}).json()["id"]
    task = api.req("POST", "/api/v2", json={"action": "create", "kind": "task", "data": {
        "company": company, "title": "Target task", "due": "2027-01-15", "assignee": "owner", "evidence": [doc]}}).json()["id"]
    client = api.req("POST", "/api/v2", json={"action": "member", "name": "Target client", "role": "Client", "companies": [company]}).json()["id"]
    return {"company": company, "doc": doc, "task": task, "client": client}


def measure(api: Api, probe: Probe, label: str, call, repeat: int = 5) -> dict:
    times, sizes, queries = [], [], []
    for _ in range(repeat):
        before = probe.count
        start = time.perf_counter()
        response = call()
        times.append(time.perf_counter() - start)
        queries.append(probe.count - before)
        sizes.append(len(response.content))
    tracemalloc.start()
    call()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"request": label, "ms": round(statistics.median(times) * 1000, 1), "queries": int(statistics.median(queries)),
            "kb": round(statistics.median(sizes) / 1024, 1), "peak_mb": round(peak / 1024 / 1024, 1)}


def run(n: int) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "data"
        frontend = Path(tmp) / "frontend"
        frontend.mkdir()
        (frontend / "index.html").write_text("<!doctype html>")
        settings = Settings(governance_data_dir=data, governance_frontend_dir=frontend, governance_allowed_hosts="testserver",
                            governance_reminders_enabled=False, _env_file=None)
        app = create_app(settings)
        with TestClient(app, base_url="http://testserver") as client:
            services = app.state.services
            seed(services.db.path, n)
            probe = Probe(services.db.engine)
            api = Api(client)
            api.sign_in()
            target = build_target(api)
            c, task, doc = target["company"], target["task"], target["doc"]
            results = []

            def edit():
                record = state_record(task)
                return api.req("POST", "/api/v2", json={"action": "edit", "id": task, "revision": record["revision"], "data": {"notes": "edited"}})

            def state_record(record_id):
                # Read the current revision straight from the database so only the action itself is measured.
                with services.db.read() as session:
                    return RecordRepository(session, WORKSPACE).get(record_id)

            def comment():
                return api.req("POST", "/api/v2", json={"action": "comment", "id": task, "revision": state_record(task)["revision"], "content": "Note"})

            def create_task():
                return api.req("POST", "/api/v2", json={"action": "create", "kind": "task", "data": {
                    "company": c, "title": "New task", "due": "2027-02-01", "assignee": "owner", "evidence": [doc]}})

            def upload():
                return api.req("POST", "/api/v2-files", data={"company": c}, files={"file": ("note.txt", b"note")})

            results.append(measure(api, probe, "GET /api/v2 (owner, full state)", lambda: api.req("GET", "/api/v2"), repeat=3))
            results.append(measure(api, probe, "POST /api/v2 edit task", edit))
            results.append(measure(api, probe, "POST /api/v2 comment", comment))
            results.append(measure(api, probe, "POST /api/v2 create task", create_task))
            results.append(measure(api, probe, "POST /api/v2-files upload", upload))
            results.append(measure(api, probe, "GET /api/v2-files download", lambda: api.req("GET", f"/api/v2-files?id={doc}")))
            results.append(measure(api, probe, "GET /api/portal (owner)", lambda: api.req("GET", "/api/portal"), repeat=3))
            results.append(measure(api, probe, "GET /api/v2/records?company=…&limit=50",
                                   lambda: api.req("GET", f"/api/v2/records?company={c}&limit=50")))
            results.append(measure(api, probe, "GET /api/v2/records?kind=task&limit=50",
                                   lambda: api.req("GET", "/api/v2/records?kind=task&limit=50")))
            results.append(measure(api, probe, "GET /api/v2/audit?limit=50", lambda: api.req("GET", "/api/v2/audit?limit=50")))
            api.role(target["client"])
            results.append(measure(api, probe, "GET /api/v2 (client, 1 company)", lambda: api.req("GET", "/api/v2")))
            results.append(measure(api, probe, "GET /api/portal (client)", lambda: api.req("GET", "/api/portal")))
            return results


def main(argv: list[str]) -> None:
    out = None
    if "--json" in argv:
        out = Path(argv[argv.index("--json") + 1])
        argv = [a for a in argv if a not in ("--json", str(out))]
    sizes = [int(a) for a in argv] or [1000, 10000, 50000, 100000]
    report = {}
    for n in sizes:
        report[n] = run(n)
        print(f"\n=== {n:,} unrelated records ===")
        print(f"{'request':44} {'ms':>9} {'SQL':>5} {'KB':>10} {'peak MB':>8}")
        for r in report[n]:
            print(f"{r['request']:44} {r['ms']:>9} {r['queries']:>5} {r['kb']:>10} {r['peak_mb']:>8}")
    if out:
        out.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
