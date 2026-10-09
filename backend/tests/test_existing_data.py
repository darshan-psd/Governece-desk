"""The FastAPI backend must keep working on data created by the Node backend."""
import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient

from app.core.config import PROJECT_DIR
from app.main import create_app
from tests.conftest import Api

MIGRATION = PROJECT_DIR / "drizzle" / "0000_redundant_luke_cage.sql"


def test_reads_and_updates_a_node_created_workspace(settings):
    data_dir = settings.governance_data_dir
    (data_dir / "files").mkdir(parents=True)
    db = sqlite3.connect(data_dir / "preview.sqlite")
    db.executescript(MIGRATION.read_text().replace("--> statement-breakpoint", ""))
    content = b"Board minutes from the Node backend"
    rows = [
        ("owner", "member", {"id": "owner", "name": "Workspace owner", "role": "Owner", "companies": ["*"], "active": True, "preview": True}),
        ("c-node", "company", {"name": "Node Company", "jurisdiction": "India", "status": "Not started", "evidence": []}),
        ("d-node", "document", {"name": "minutes.txt", "company": "c-node", "key": "local-preview/d-node", "size": len(content),
                                "hash": hashlib.sha256(content).hexdigest(), "version": 1, "previous": None, "shared": False,
                                "category": "Other", "uploadedBy": "owner", "scanStatus": "Not scanned"}),
    ]
    for record_id, kind, data in rows:
        db.execute("INSERT INTO records (owner,id,kind,data,revision,updated) VALUES (?,?,?,?,1,?)",
                   ("local-preview", record_id, kind, json.dumps(data), "2026-10-01T10:00:00.000Z"))
    db.execute("INSERT INTO audit VALUES ('a1','local-preview','Created company · Workspace owner','Node Company','2026-10-01T10:00:00.000Z')")
    db.commit()
    db.close()
    # The Node adapter stored objects as files/<encodeURIComponent(key)>.
    (data_dir / "files" / "local-preview%2Fd-node").write_bytes(content)

    with TestClient(create_app(settings), base_url="http://testserver") as client:
        api = Api(client).sign_in()
        state = api.state()
        assert {r["id"] for r in state["records"]} == {"c-node", "d-node"}
        assert state["audit"][0]["subject"] == "Node Company"
        assert api.request("GET", "/api/v2-files?id=d-node").content == content
        api.edit("c-node", {"name": "Node Company Ltd"})
        assert api.get("c-node")["revision"] == 2
        task = api.create("task", {"company": "c-node", "title": "New", "due": "2026-12-01", "evidence": ["d-node"]})
        assert api.get(task)["evidence"] == ["d-node"]
    with sqlite3.connect(data_dir / "preview.sqlite") as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"records", "audit", "sessions"} <= tables
