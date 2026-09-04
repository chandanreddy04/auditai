"""
Tests the coordination behavior itself - step ordering, skip-on-failure,
and run-status rollup - not the underlying reconciliation/controls math,
which already has its own dedicated test files. Uses a real test-DB
session (these functions read/write real rows) with the LLM-touching
extraction call mocked, same pattern as test_api.py's flow tests.
"""

from app.database.session import SessionLocal, init_db
from app.models.models import (
    Client, Control, ControlRuleType, Document, Engagement, EvidenceRecord,
    OrchestrationRunStatus, OrchestrationStepStatus, OrchestrationTrigger,
)
from app.schemas.extraction import LLMExtractedEvidence
from app.services import orchestration_service as svc

init_db()


def make_engagement(db):
    client = Client(name="Orchestration Test Corp")
    db.add(client)
    db.commit()
    engagement = Engagement(client_id=client.id, name="Orchestration Test Engagement", audit_type="financial")
    db.add(engagement)
    db.commit()
    return engagement


def make_document(db, engagement, filename="test.pdf"):
    doc = Document(engagement_id=engagement.id, client_id=engagement.client_id, filename=filename, file_path="unused")
    db.add(doc)
    db.commit()
    return doc


def test_run_document_pipeline_success_runs_all_four_steps_in_order(monkeypatch):
    db = SessionLocal()
    engagement = make_engagement(db)
    document = make_document(db, engagement)

    monkeypatch.setattr(svc, "extract_text", lambda path: "INVOICE\nInvoice Number: INV-1\nAmount: 100.00")
    monkeypatch.setattr(svc, "extract_evidence", lambda text: LLMExtractedEvidence(
        doc_type="invoice", vendor_name="Acme", reference_number="INV-1", related_reference_number=None,
        amount=100.0, currency="USD", record_date="2026-01-01", approver_name=None,
    ))

    run = svc.run_document_pipeline(db, document, engagement)

    assert run.trigger == OrchestrationTrigger.DOCUMENT_UPLOAD
    assert run.triggered_by == "test.pdf"
    assert run.status == OrchestrationRunStatus.COMPLETED
    assert len(run.steps) == 4
    assert [s.agent_name for s in run.steps] == [
        "evidence_extraction_step", "reconciliation_step", "fraud_risk_step", "controls_testing_step",
    ]
    assert [s.step_order for s in run.steps] == [1, 2, 3, 4]
    assert run.steps[0].status == OrchestrationStepStatus.SUCCESS
    assert run.steps[1].status == OrchestrationStepStatus.SUCCESS       # 1 evidence record now exists -> reconciliation runs
    assert run.steps[2].status == OrchestrationStepStatus.SUCCESS       # fraud-risk always runs when evidence exists
    assert run.steps[3].status == OrchestrationStepStatus.SKIPPED       # no controls defined for this engagement

    evidence = db.query(EvidenceRecord).filter(EvidenceRecord.document_id == document.id).first()
    assert evidence is not None
    assert evidence.reference_number == "INV-1"
    db.close()


def test_run_document_pipeline_with_active_control_reports_success(monkeypatch):
    db = SessionLocal()
    engagement = make_engagement(db)
    document = make_document(db, engagement)
    db.add(Control(engagement_id=engagement.id, client_id=engagement.client_id, name="PO required", rule_type=ControlRuleType.PO_REQUIRED_ABOVE_THRESHOLD, threshold_amount=50.0))
    db.commit()

    monkeypatch.setattr(svc, "extract_text", lambda path: "INVOICE\nInvoice Number: INV-1\nAmount: 100.00")
    monkeypatch.setattr(svc, "extract_evidence", lambda text: LLMExtractedEvidence(
        doc_type="invoice", vendor_name="Acme", reference_number="INV-2", related_reference_number=None,
        amount=100.0, currency="USD", record_date="2026-01-01", approver_name=None,
    ))

    run = svc.run_document_pipeline(db, document, engagement)

    assert run.status == OrchestrationRunStatus.COMPLETED
    assert run.steps[3].agent_name == "controls_testing_step"
    assert run.steps[3].status == OrchestrationStepStatus.SUCCESS
    assert "1 results" in run.steps[3].detail
    db.close()


def test_run_document_pipeline_extraction_failure_skips_downstream_steps(monkeypatch):
    from app.services.llm_client import LLMUnavailableError

    db = SessionLocal()
    engagement = make_engagement(db)
    document = make_document(db, engagement)

    monkeypatch.setattr(svc, "extract_text", lambda path: "INVOICE\nInvoice Number: INV-1\nAmount: 100.00")
    def boom(text):
        raise LLMUnavailableError("model down")
    monkeypatch.setattr(svc, "extract_evidence", boom)

    run = svc.run_document_pipeline(db, document, engagement)

    assert run.status == OrchestrationRunStatus.FAILED
    assert len(run.steps) == 4
    assert run.steps[0].status == OrchestrationStepStatus.FAILED
    assert run.steps[1].status == OrchestrationStepStatus.SKIPPED
    assert run.steps[2].status == OrchestrationStepStatus.SKIPPED
    assert run.steps[3].status == OrchestrationStepStatus.SKIPPED
    assert "extraction did not succeed" in run.steps[1].detail
    db.close()


def test_run_document_pipeline_scanned_pdf_falls_back_to_vision_and_succeeds(monkeypatch):
    """No text layer used to mean SKIPPED and stop there. Now it means
    'render the page and ask the vision model instead' - and if that
    works, extraction is a real SUCCESS, not a dead end."""
    db = SessionLocal()
    engagement = make_engagement(db)
    document = make_document(db, engagement)

    monkeypatch.setattr(svc, "extract_text", lambda path: "")  # empty -> no extractable text
    monkeypatch.setattr(svc, "render_pdf_page_to_image", lambda path: b"fake-rendered-png")
    monkeypatch.setattr(svc, "extract_evidence_from_image", lambda image_bytes: LLMExtractedEvidence(
        doc_type="invoice", vendor_name="Scanned Vendor", reference_number="INV-SCAN-1",
        related_reference_number=None, amount=250.0, currency="USD", record_date=None, approver_name=None,
    ))

    run = svc.run_document_pipeline(db, document, engagement)

    assert run.steps[0].status == OrchestrationStepStatus.SUCCESS
    assert "INV-SCAN-1" in run.steps[0].detail
    assert run.status == OrchestrationRunStatus.COMPLETED
    assert run.steps[1].status == OrchestrationStepStatus.SUCCESS  # 1 evidence record now exists
    db.close()


def test_run_document_pipeline_scanned_pdf_vision_unavailable_fails(monkeypatch):
    from app.services.llm_client import LLMUnavailableError

    db = SessionLocal()
    engagement = make_engagement(db)
    document = make_document(db, engagement)

    monkeypatch.setattr(svc, "extract_text", lambda path: "")
    monkeypatch.setattr(svc, "render_pdf_page_to_image", lambda path: b"fake-rendered-png")
    def boom(image_bytes):
        raise LLMUnavailableError("vision model down")
    monkeypatch.setattr(svc, "extract_evidence_from_image", boom)

    run = svc.run_document_pipeline(db, document, engagement)

    assert run.steps[0].status == OrchestrationStepStatus.FAILED
    assert run.status == OrchestrationRunStatus.FAILED
    assert run.steps[1].status == OrchestrationStepStatus.SKIPPED
    db.close()


def test_run_document_pipeline_image_upload_uses_vision_directly_not_pdf_text(monkeypatch, tmp_path):
    """A directly-uploaded JPG/PNG should never go through extract_text()
    at all - only through the vision path."""
    db = SessionLocal()
    engagement = make_engagement(db)
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"fake-jpeg-bytes")
    document = Document(engagement_id=engagement.id, client_id=engagement.client_id, filename="photo.jpg", file_path=str(image_path))
    db.add(document)
    db.commit()

    def extract_text_should_not_be_called(path):
        raise AssertionError("extract_text() should never be called for an image upload")
    monkeypatch.setattr(svc, "extract_text", extract_text_should_not_be_called)
    monkeypatch.setattr(svc, "extract_evidence_from_image", lambda image_bytes: LLMExtractedEvidence(
        doc_type="invoice", vendor_name="Photo Vendor", reference_number="INV-PHOTO-1",
        related_reference_number=None, amount=75.0, currency="USD", record_date=None, approver_name=None,
    ))

    run = svc.run_document_pipeline(db, document, engagement)

    assert run.steps[0].status == OrchestrationStepStatus.SUCCESS
    assert "INV-PHOTO-1" in run.steps[0].detail
    db.close()


def test_run_full_engagement_check_runs_three_steps(monkeypatch):
    db = SessionLocal()
    engagement = make_engagement(db)
    document = make_document(db, engagement)
    db.add(EvidenceRecord(
        document_id=document.id, engagement_id=engagement.id, client_id=engagement.client_id,
        doc_type="invoice", reference_number="INV-3", related_reference_number=None, amount=200.0,
    ))
    db.commit()

    run = svc.run_full_engagement_check(db, engagement, triggered_by="Test Auditor")

    assert run.trigger == OrchestrationTrigger.MANUAL
    assert run.triggered_by == "Test Auditor"
    assert len(run.steps) == 3
    assert [s.agent_name for s in run.steps] == ["reconciliation_step", "fraud_risk_step", "controls_testing_step"]
    assert run.status == OrchestrationRunStatus.COMPLETED
    db.close()


def test_run_reconciliation_step_skips_when_no_evidence():
    db = SessionLocal()
    engagement = make_engagement(db)
    outcome = svc.run_reconciliation_step(db, engagement)
    assert outcome.status == OrchestrationStepStatus.SKIPPED
    db.close()


def test_run_fraud_risk_step_skips_when_no_evidence():
    db = SessionLocal()
    engagement = make_engagement(db)
    outcome = svc.run_fraud_risk_step(db, engagement)
    assert outcome.status == OrchestrationStepStatus.SKIPPED
    db.close()


def test_run_fraud_risk_step_reports_success_and_persists_flags():
    db = SessionLocal()
    engagement = make_engagement(db)
    document = make_document(db, engagement)
    db.add(EvidenceRecord(
        document_id=document.id, engagement_id=engagement.id, client_id=engagement.client_id,
        doc_type="invoice", vendor_name="Brand New Vendor", reference_number="INV-5", amount=10000.0,
    ))
    db.commit()

    outcome = svc.run_fraud_risk_step(db, engagement)
    assert outcome.status == OrchestrationStepStatus.SUCCESS
    # amount=10000.0 with a single-appearance vendor trips BOTH
    # new_vendor_large_amount and round_dollar_amount on this one record.
    assert "2 new" in outcome.detail

    from app.models.models import FraudRiskFlag
    flags = db.query(FraudRiskFlag).filter(FraudRiskFlag.engagement_id == engagement.id).all()
    assert len(flags) == 2
    db.close()


def test_run_controls_testing_step_skips_when_no_controls():
    db = SessionLocal()
    engagement = make_engagement(db)
    document = make_document(db, engagement)
    db.add(EvidenceRecord(
        document_id=document.id, engagement_id=engagement.id, client_id=engagement.client_id,
        doc_type="invoice", reference_number="INV-4", amount=10.0,
    ))
    db.commit()
    outcome = svc.run_controls_testing_step(db, engagement)
    assert outcome.status == OrchestrationStepStatus.SKIPPED
    db.close()
