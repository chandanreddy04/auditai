from fastapi.testclient import TestClient

from app.database.session import init_db
from app.main import app

init_db()
client = TestClient(app)

# Every route except /health, /login, /signup now requires a logged-in
# user (see app/web/auth_routes.py). TestClient's cookie jar persists
# across requests made on this same client instance, so signing up
# once here authenticates every request the rest of this file makes.
client.post("/signup", data={"name": "Test Auditor", "email": "test-auditor@example.com", "password": "testpassword123"})


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


def test_controls_testing_flow():
    from app.database.session import SessionLocal
    from app.models.models import Client, ControlTestResult, Document, DocumentType, Engagement, EvidenceRecord

    client.post("/clients", data={"name": "Controls Test Corp"}, follow_redirects=False)
    db = SessionLocal()
    acme = db.query(Client).filter(Client.name == "Controls Test Corp").first()
    db.close()

    client.post(
        f"/clients/{acme.id}/engagements",
        data={"name": "Controls Engagement", "audit_type": "financial"},
        follow_redirects=False,
    )
    db = SessionLocal()
    engagement = db.query(Engagement).filter(Engagement.client_id == acme.id).first()
    db.close()

    # Seed one evidence record directly (bypassing the LLM - this test is
    # about the controls engine and route wiring, not extraction) that
    # should FAIL a "PO required above $1,000" control: an invoice with no
    # related_reference_number.
    db = SessionLocal()
    doc = Document(engagement_id=engagement.id, client_id=acme.id, filename="test_invoice.pdf", file_path="x")
    db.add(doc)
    db.commit()
    record = EvidenceRecord(
        document_id=doc.id, engagement_id=engagement.id, client_id=acme.id,
        doc_type=DocumentType.INVOICE, reference_number="INV-999", related_reference_number=None, amount=5000.0,
    )
    db.add(record)
    db.commit()
    db.close()

    resp = client.post(
        f"/engagements/{engagement.id}/controls",
        data={"name": "POs required over $1,000", "rule_type": "po_required_above_threshold", "threshold_amount": "1000"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get(f"/engagements/{engagement.id}/controls")
    assert resp.status_code == 200
    assert "POs required over $1,000" in resp.text
    assert "INV-999" in resp.text  # the open finding's detail names the invoice

    db = SessionLocal()
    open_result = db.query(ControlTestResult).filter(ControlTestResult.engagement_id == engagement.id).first()
    db.close()
    assert open_result is not None
    assert open_result.result.value == "fail"
    assert open_result.status.value == "open"

    resp = client.post(
        f"/control-results/{open_result.id}/resolve",
        data={"resolution_note": "Investigated - compensating control found.", "action": "resolved"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = SessionLocal()
    db.expire_all()
    resolved = db.get(ControlTestResult, open_result.id)
    db.close()
    assert resolved.status.value == "resolved"
    assert resolved.resolved_by == "Test Auditor"

    # Dashboard's open_control_failures count should reflect the resolve.
    resp = client.get(f"/engagements/{engagement.id}")
    assert resp.status_code == 200


def test_workpaper_flow(monkeypatch):
    from app.database.session import SessionLocal
    from app.models.models import Client, Engagement, Workpaper
    from app.web import routes as routes_module

    monkeypatch.setattr(routes_module.workpaper_service, "draft_workpaper_narrative", lambda summary: "This is a drafted memo.")

    client.post("/clients", data={"name": "Workpaper Test Corp"}, follow_redirects=False)
    db = SessionLocal()
    acme = db.query(Client).filter(Client.name == "Workpaper Test Corp").first()
    db.close()

    client.post(
        f"/clients/{acme.id}/engagements",
        data={"name": "Workpaper Engagement", "audit_type": "financial"},
        follow_redirects=False,
    )
    db = SessionLocal()
    engagement = db.query(Engagement).filter(Engagement.client_id == acme.id).first()
    db.close()

    # First visit auto-creates an empty draft workpaper.
    resp = client.get(f"/engagements/{engagement.id}/workpaper")
    assert resp.status_code == 200
    assert "draft" in resp.text.lower()

    # Cannot finalize an empty draft.
    resp = client.post(f"/engagements/{engagement.id}/workpaper/finalize")
    assert resp.status_code == 400

    # Generate (LLM call mocked above) -> draft content appears.
    resp = client.post(f"/engagements/{engagement.id}/workpaper/generate", follow_redirects=False)
    assert resp.status_code == 303

    resp = client.get(f"/engagements/{engagement.id}/workpaper")
    assert "This is a drafted memo." in resp.text

    # Human edits are saved.
    resp = client.post(
        f"/engagements/{engagement.id}/workpaper/save",
        data={"content": "This is a drafted memo, edited by a human."},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    resp = client.get(f"/engagements/{engagement.id}/workpaper")
    assert "edited by a human" in resp.text

    # Finalize locks it.
    resp = client.post(
        f"/engagements/{engagement.id}/workpaper/finalize",
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = SessionLocal()
    db.expire_all()
    wp = db.query(Workpaper).filter(Workpaper.engagement_id == engagement.id).first()
    db.close()
    assert wp.status.value == "finalized"
    assert wp.finalized_by == "Test Auditor"

    # A finalized workpaper refuses further edits and regeneration.
    resp = client.post(f"/engagements/{engagement.id}/workpaper/save", data={"content": "should not apply"})
    assert resp.status_code == 400
    resp = client.post(f"/engagements/{engagement.id}/workpaper/generate")
    assert resp.status_code == 400


def test_pbc_flow(monkeypatch):
    from datetime import date, timedelta

    from app.database.session import SessionLocal
    from app.models.models import Client, Engagement, PBCRequest
    from app.web import routes as routes_module

    monkeypatch.setattr(routes_module.pbc_service, "draft_reminder_email", lambda client_name, engagement_name, overdue: "Please send the overdue items.")

    client.post("/clients", data={"name": "PBC Test Corp"}, follow_redirects=False)
    db = SessionLocal()
    acme = db.query(Client).filter(Client.name == "PBC Test Corp").first()
    db.close()

    client.post(
        f"/clients/{acme.id}/engagements",
        data={"name": "PBC Engagement", "audit_type": "financial"},
        follow_redirects=False,
    )
    db = SessionLocal()
    engagement = db.query(Engagement).filter(Engagement.client_id == acme.id).first()
    db.close()

    resp = client.get(f"/engagements/{engagement.id}/pbc")
    assert resp.status_code == 200

    overdue_due = (date.today() - timedelta(days=5)).isoformat()
    future_due = (date.today() + timedelta(days=5)).isoformat()

    resp = client.post(
        f"/engagements/{engagement.id}/pbc",
        data={"item_name": "Bank statements", "description": "Q1-Q4", "due_date": overdue_due},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    client.post(
        f"/engagements/{engagement.id}/pbc",
        data={"item_name": "Lease agreements", "due_date": future_due},
        follow_redirects=False,
    )

    resp = client.get(f"/engagements/{engagement.id}/pbc")
    assert "Bank statements" in resp.text
    assert "overdue" in resp.text.lower()
    assert "Draft reminder email" in resp.text  # only shown because an overdue item exists

    db = SessionLocal()
    bank_item = db.query(PBCRequest).filter(PBCRequest.item_name == "Bank statements").first()
    lease_item = db.query(PBCRequest).filter(PBCRequest.item_name == "Lease agreements").first()
    db.close()

    # Draft a reminder (LLM mocked) - rendered directly, not persisted.
    resp = client.post(f"/engagements/{engagement.id}/pbc/draft-reminder")
    assert resp.status_code == 200
    assert "Please send the overdue items." in resp.text

    # Mark the overdue one received.
    resp = client.post(
        f"/pbc/{bank_item.id}/receive",
        data={"resolution_note": "Received via email", "linked_document_id": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Waive the other one.
    resp = client.post(
        f"/pbc/{lease_item.id}/waive",
        data={"resolution_note": "Not needed for this scope"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = SessionLocal()
    db.expire_all()
    bank_item = db.get(PBCRequest, bank_item.id)
    lease_item = db.get(PBCRequest, lease_item.id)
    db.close()
    assert bank_item.status.value == "received"
    assert lease_item.status.value == "waived"

    resp = client.get(f"/engagements/{engagement.id}/pbc")
    assert resp.status_code == 200
    assert "Open requests (0)" in resp.text
    assert "Closed (2)" in resp.text


def test_orchestration_flow(monkeypatch):
    from app.database.session import SessionLocal
    from app.models.models import Client, Engagement, OrchestrationRun
    from app.schemas.extraction import LLMExtractedEvidence
    from app.web import routes as routes_module

    monkeypatch.setattr(
        routes_module.orchestration_service, "extract_text",
        lambda path: "INVOICE\nInvoice Number: INV-ORCH-1\nAmount: 100.00",
    )
    monkeypatch.setattr(
        routes_module.orchestration_service, "extract_evidence",
        lambda text: LLMExtractedEvidence(
            doc_type="invoice", vendor_name="Acme", reference_number="INV-ORCH-1", related_reference_number=None,
            amount=100.0, currency="USD", record_date="2026-01-01", approver_name=None,
        ),
    )

    client.post("/clients", data={"name": "Orchestration Test Corp"}, follow_redirects=False)
    db = SessionLocal()
    acme = db.query(Client).filter(Client.name == "Orchestration Test Corp").first()
    db.close()

    client.post(
        f"/clients/{acme.id}/engagements",
        data={"name": "Orchestration Engagement", "audit_type": "financial"},
        follow_redirects=False,
    )
    db = SessionLocal()
    engagement = db.query(Engagement).filter(Engagement.client_id == acme.id).first()
    db.close()

    resp = client.get(f"/engagements/{engagement.id}/orchestration")
    assert resp.status_code == 200
    assert "No orchestration runs yet" in resp.text

    # Upload a document - the pipeline should run automatically (extraction mocked above).
    resp = client.post(
        f"/engagements/{engagement.id}/documents",
        files={"file": ("orch_test.pdf", b"%PDF-1.4 fake", "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get(f"/engagements/{engagement.id}/orchestration")
    assert resp.status_code == 200
    assert "document_upload" in resp.text
    assert "evidence_extraction_agent" in resp.text
    assert "reconciliation_agent" in resp.text
    assert "controls_testing_agent" in resp.text

    db = SessionLocal()
    run = db.query(OrchestrationRun).filter(OrchestrationRun.engagement_id == engagement.id).first()
    assert run is not None
    assert run.trigger.value == "document_upload"
    assert run.triggered_by == "orch_test.pdf"
    assert len(run.steps) == 3
    db.close()

    # Manual full-check trigger.
    resp = client.post(f"/engagements/{engagement.id}/run-full-check", follow_redirects=False)
    assert resp.status_code == 303

    resp = client.get(f"/engagements/{engagement.id}/orchestration")
    assert "manual" in resp.text
    assert "Test Auditor" in resp.text

    db = SessionLocal()
    runs = db.query(OrchestrationRun).filter(OrchestrationRun.engagement_id == engagement.id).all()
    assert len(runs) == 2
    manual_run = [r for r in runs if r.trigger.value == "manual"][0]
    assert len(manual_run.steps) == 2  # no extraction step for a manual re-check
    db.close()


def test_missing_engagement_is_404():
    resp = client.get("/engagements/999999")
    assert resp.status_code == 404


def test_missing_client_is_404():
    resp = client.get("/clients/999999")
    assert resp.status_code == 404
