import json
from types import SimpleNamespace

import pytest

from app.models.models import (
    ControlTestStatus, DocumentStatus, DocumentType, ExceptionStatus, ExceptionType,
)
from app.services import workpaper_service as svc
from app.services.llm_client import LLMUnavailableError


def make_engagement(name="FY2026 Audit", client_name="Acme Corp", audit_type="financial"):
    return SimpleNamespace(name=name, audit_type=audit_type, client=SimpleNamespace(name=client_name))


def make_document(doc_type=DocumentType.INVOICE, status=DocumentStatus.EXTRACTED):
    return SimpleNamespace(doc_type=doc_type, status=status)


def make_evidence(doc_type=DocumentType.INVOICE, amount=100.0):
    return SimpleNamespace(doc_type=doc_type, amount=amount)


def make_exception(exception_type=ExceptionType.MISSING_MATCH, status=ExceptionStatus.OPEN, description="desc", resolved_by=None, resolution_note=None):
    return SimpleNamespace(exception_type=exception_type, status=status, description=description, resolved_by=resolved_by, resolution_note=resolution_note)


def make_control_result(control_id=1, control_name="POs required", result=ControlTestStatus.PASS, status=ExceptionStatus.RESOLVED, detail="ok", resolved_by=None, resolution_note=None):
    return SimpleNamespace(
        control_id=control_id, control=SimpleNamespace(name=control_name),
        result=result, status=status, detail=detail, resolved_by=resolved_by, resolution_note=resolution_note,
    )


# --------------------------------------------------------------- _summarize

def test_summarize_basic_counts():
    engagement = make_engagement()
    documents = [make_document(DocumentType.PURCHASE_ORDER), make_document(DocumentType.INVOICE)]
    evidence = [make_evidence(DocumentType.PURCHASE_ORDER, 1000.0), make_evidence(DocumentType.INVOICE, 1000.0)]
    exceptions = []
    control_results = []

    summary = svc._summarize(engagement, documents, evidence, exceptions, control_results)

    assert summary.engagement_name == "FY2026 Audit"
    assert summary.client_name == "Acme Corp"
    assert summary.documents_total == 2
    assert summary.documents_by_type == {"purchase_order": 1, "invoice": 1}
    assert summary.evidence_total == 2
    assert summary.evidence_amount_by_type == {"purchase_order": 1000.0, "invoice": 1000.0}


def test_summarize_documents_failed_count():
    documents = [make_document(status=DocumentStatus.FAILED), make_document(status=DocumentStatus.EXTRACTED)]
    summary = svc._summarize(make_engagement(), documents, [], [], [])
    assert summary.documents_failed == 1


def test_summarize_exception_status_breakdown():
    exceptions = [
        make_exception(status=ExceptionStatus.OPEN),
        make_exception(status=ExceptionStatus.RESOLVED, resolved_by="Jane", resolution_note="Fixed it"),
        make_exception(status=ExceptionStatus.DISMISSED),
    ]
    summary = svc._summarize(make_engagement(), [], [], exceptions, [])
    assert summary.exceptions_total == 3
    assert summary.exceptions_open == 1
    assert summary.exceptions_resolved == 1
    assert summary.exceptions_dismissed == 1
    assert len(summary.exception_details) == 3
    resolved_detail = [d for d in summary.exception_details if d.status == "resolved"][0]
    assert resolved_detail.resolved_by == "Jane"
    assert resolved_detail.resolution_note == "Fixed it"


def test_summarize_control_results_breakdown():
    control_results = [
        make_control_result(control_id=1, result=ControlTestStatus.PASS, status=ExceptionStatus.RESOLVED),
        make_control_result(control_id=1, result=ControlTestStatus.FAIL, status=ExceptionStatus.OPEN),
        make_control_result(control_id=2, result=ControlTestStatus.FAIL, status=ExceptionStatus.RESOLVED, resolved_by="Tom"),
    ]
    summary = svc._summarize(make_engagement(), [], [], [], control_results)
    assert summary.control_results_pass == 1
    assert summary.control_results_fail_open == 1
    assert summary.control_results_fail_closed == 1
    assert summary.controls_defined == 2  # distinct control_ids
    assert len(summary.control_findings) == 3


def test_summarize_empty_engagement_produces_zeroed_summary():
    summary = svc._summarize(make_engagement(), [], [], [], [])
    assert summary.documents_total == 0
    assert summary.evidence_total == 0
    assert summary.exceptions_total == 0
    assert summary.controls_defined == 0
    assert summary.control_findings == []


# ----------------------------------------------------- draft_workpaper_narrative

def test_draft_workpaper_narrative_returns_stripped_text(monkeypatch):
    monkeypatch.setattr(svc, "chat", lambda **kwargs: "  A clean draft memo.  \n")
    summary = svc._summarize(make_engagement(), [], [], [], [])
    result = svc.draft_workpaper_narrative(summary)
    assert result == "A clean draft memo."


def test_draft_workpaper_narrative_propagates_llm_unavailable(monkeypatch):
    def boom(**kwargs):
        raise LLMUnavailableError("model down")
    monkeypatch.setattr(svc, "chat", boom)

    summary = svc._summarize(make_engagement(), [], [], [], [])
    with pytest.raises(LLMUnavailableError):
        svc.draft_workpaper_narrative(summary)


def test_format_summary_for_prompt_includes_key_facts():
    exceptions = [make_exception(description="Invoice X has no PO", resolved_by="Jane", resolution_note="Verified fine")]
    control_results = [make_control_result(control_name="Approval required", detail="INV-1 missing approver")]
    summary = svc._summarize(make_engagement(), [], [], exceptions, control_results)

    text = svc._format_summary_for_prompt(summary)
    assert "Invoice X has no PO" in text
    assert "Verified fine" in text
    assert "Approval required" in text
    assert "INV-1 missing approver" in text
