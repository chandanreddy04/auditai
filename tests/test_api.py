from fastapi.testclient import TestClient

from app.database.session import init_db
from app.main import app

init_db()
client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "llm_available" in resp.json()


def test_home_page_lists_clients():
    resp = client.get("/")
    assert resp.status_code == 200


def test_full_flow_client_engagement_dashboard():
    resp = client.post("/clients", data={"name": "Acme Test Corp"}, follow_redirects=False)
    assert resp.status_code == 303

    resp = client.get("/")
    assert "Acme Test Corp" in resp.text

    # Find the newly created client's id via the clients list (SQLite autoincrement -> just query the DB)
    from app.database.session import SessionLocal
    from app.models.models import Client
    db = SessionLocal()
    acme = db.query(Client).filter(Client.name == "Acme Test Corp").first()
    db.close()
    assert acme is not None

    resp = client.get(f"/clients/{acme.id}")
    assert resp.status_code == 200

    resp = client.post(
        f"/clients/{acme.id}/engagements",
        data={"name": "FY2026 Financial Audit", "audit_type": "financial"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from app.models.models import Engagement
    db = SessionLocal()
    engagement = db.query(Engagement).filter(Engagement.client_id == acme.id).first()
    db.close()
    assert engagement is not None

    resp = client.get(f"/engagements/{engagement.id}")
    assert resp.status_code == 200
    assert "FY2026 Financial Audit" in resp.text

    resp = client.get(f"/engagements/{engagement.id}/exceptions")
    assert resp.status_code == 200

    resp = client.get(f"/engagements/{engagement.id}/audit-log")
    assert resp.status_code == 200
    # "engagement_created" belongs to this engagement's log. "client_created"
    # deliberately does NOT show here - it has no engagement_id at all (a
    # client isn't scoped to one engagement), so it's absent by design, not
    # by bug - confirmed separately below via a direct DB query.
    assert "engagement_created" in resp.text

    from app.models.models import AuditLogEntry
    db = SessionLocal()
    client_created_entry = db.query(AuditLogEntry).filter(AuditLogEntry.action == "client_created").first()
    db.close()
    assert client_created_entry is not None
    assert client_created_entry.engagement_id is None


def test_missing_engagement_is_404():
    resp = client.get("/engagements/999999")
    assert resp.status_code == 404


def test_missing_client_is_404():
    resp = client.get("/clients/999999")
    assert resp.status_code == 404
