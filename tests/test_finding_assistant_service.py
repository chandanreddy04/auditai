import json
from types import SimpleNamespace

import pytest

from app.models.models import ControlTestStatus, ExceptionStatus, ExceptionType, FindingRiskRating, FindingSourceType
from app.services import finding_assistant_service as svc
from app.services.llm_client import LLMUnavailableError


def make_exception(id=1, exception_type=ExceptionType.MISSING_MATCH, status=ExceptionStatus.OPEN, description="desc", resolved_by=None, resolution_note=None):
    return SimpleNamespace(id=id, exception_type=exception_type, status=status, description=description, resolved_by=resolved_by, resolution_note=resolution_note)


def make_control_result(id=1, control_name="POs required", result=ControlTestStatus.FAIL, status=ExceptionStatus.OPEN, detail="detail", resolved_by=None, resolution_note=None):
    return SimpleNamespace(
        id=id, control=SimpleNamespace(name=control_name),
        result=result, status=status, detail=detail, resolved_by=resolved_by, resolution_note=resolution_note,
    )


def make_fraud_flag(id=1, flag_type="round_dollar_amount", status=ExceptionStatus.OPEN, description="desc", severity="medium", resolved_by=None, resolution_note=None):
    return SimpleNamespace(id=id, flag_type=SimpleNamespace(value=flag_type), status=status, description=description, severity=severity, resolved_by=resolved_by, resolution_note=resolution_note)


def make_finding(source_type, source_id):
    return SimpleNamespace(source_type=source_type, source_id=source_id)


# ------------------------------------------------------- gather_finding_candidates

def test_gathers_open_and_resolved_exceptions_but_not_dismissed():
    exceptions = [
        make_exception(id=1, status=ExceptionStatus.OPEN),
        make_exception(id=2, status=ExceptionStatus.RESOLVED),
        make_exception(id=3, status=ExceptionStatus.DISMISSED),
    ]
    candidates = svc.gather_finding_candidates(exceptions, [], [], [])
    ids = {c.source_id for c in candidates}
    assert ids == {1, 2}


def test_gathers_only_failed_control_results():
    control_results = [
        make_control_result(id=1, result=ControlTestStatus.PASS),
        make_control_result(id=2, result=ControlTestStatus.FAIL),
    ]
    candidates = svc.gather_finding_candidates([], control_results, [], [])
    assert len(candidates) == 1
    assert candidates[0].source_id == 2
    assert candidates[0].source_type == FindingSourceType.CONTROL_FAILURE


def test_gathers_open_and_resolved_fraud_flags_but_not_dismissed():
    flags = [
        make_fraud_flag(id=1, status=ExceptionStatus.OPEN),
        make_fraud_flag(id=2, status=ExceptionStatus.RESOLVED),
        make_fraud_flag(id=3, status=ExceptionStatus.DISMISSED),
    ]
    candidates = svc.gather_finding_candidates([], [], flags, [])
    ids = {c.source_id for c in candidates}
    assert ids == {1, 2}


def test_dedup_skips_items_that_already_have_a_finding():
    exceptions = [make_exception(id=1), make_exception(id=2)]
    existing = [make_finding(FindingSourceType.RECONCILIATION_EXCEPTION, 1)]
    candidates = svc.gather_finding_candidates(exceptions, [], [], existing)
    assert len(candidates) == 1
    assert candidates[0].source_id == 2


def test_exception_risk_rating_mapping():
    exceptions = [
        make_exception(id=1, exception_type=ExceptionType.DUPLICATE),
        make_exception(id=2, exception_type=ExceptionType.MISSING_MATCH),
        make_exception(id=3, exception_type=ExceptionType.AMOUNT_MISMATCH),
        make_exception(id=4, exception_type=ExceptionType.UNREADABLE),
    ]
    candidates = svc.gather_finding_candidates(exceptions, [], [], [])
    by_id = {c.source_id: c.risk_rating for c in candidates}
    assert by_id[1] == FindingRiskRating.HIGH
    assert by_id[2] == FindingRiskRating.HIGH
    assert by_id[3] == FindingRiskRating.MEDIUM
    assert by_id[4] == FindingRiskRating.LOW


def test_control_failure_risk_rating_is_always_high():
    control_results = [make_control_result(id=1, result=ControlTestStatus.FAIL)]
    candidates = svc.gather_finding_candidates([], control_results, [], [])
    assert candidates[0].risk_rating == FindingRiskRating.HIGH


def test_fraud_flag_risk_rating_passes_through_severity():
    flags = [
        make_fraud_flag(id=1, severity="high"),
        make_fraud_flag(id=2, severity="low"),
        make_fraud_flag(id=3, severity="unusual-value"),
    ]
    candidates = svc.gather_finding_candidates([], [], flags, [])
    by_id = {c.source_id: c.risk_rating for c in candidates}
    assert by_id[1] == FindingRiskRating.HIGH
    assert by_id[2] == FindingRiskRating.LOW
    assert by_id[3] == FindingRiskRating.MEDIUM  # unknown value falls back to medium


def test_candidate_source_key_format():
    exceptions = [make_exception(id=42)]
    candidates = svc.gather_finding_candidates(exceptions, [], [], [])
    assert candidates[0].source_key == "reconciliation_exception:42"


# ----------------------------------------------------------------- draft_findings

def test_draft_findings_returns_empty_list_for_no_candidates():
    assert svc.draft_findings([]) == []


def test_draft_findings_parses_and_returns_matching_results(monkeypatch):
    exceptions = [make_exception(id=1)]
    candidates = svc.gather_finding_candidates(exceptions, [], [], [])

    response = {
        "findings": [{
            "source_key": "reconciliation_exception:1",
            "title": "Missing PO match",
            "root_cause": "No PO was on file.",
            "impact": "Spend cannot be verified as authorized.",
            "recommendation": "Obtain the PO or document why none exists.",
        }]
    }
    monkeypatch.setattr(svc, "chat", lambda **kwargs: json.dumps(response))

    results = svc.draft_findings(candidates)
    assert len(results) == 1
    assert results[0].source_key == "reconciliation_exception:1"
    assert results[0].title == "Missing PO match"


def test_draft_findings_filters_out_hallucinated_source_keys(monkeypatch):
    exceptions = [make_exception(id=1)]
    candidates = svc.gather_finding_candidates(exceptions, [], [], [])

    response = {
        "findings": [
            {"source_key": "reconciliation_exception:1", "title": "Real one", "root_cause": "r", "impact": "i", "recommendation": "rec"},
            {"source_key": "reconciliation_exception:999", "title": "Invented", "root_cause": "r", "impact": "i", "recommendation": "rec"},
        ]
    }
    monkeypatch.setattr(svc, "chat", lambda **kwargs: json.dumps(response))

    results = svc.draft_findings(candidates)
    assert len(results) == 1
    assert results[0].source_key == "reconciliation_exception:1"


def test_draft_findings_propagates_llm_unavailable(monkeypatch):
    def boom(**kwargs):
        raise LLMUnavailableError("model down")
    monkeypatch.setattr(svc, "chat", boom)

    candidates = svc.gather_finding_candidates([make_exception(id=1)], [], [], [])
    with pytest.raises(LLMUnavailableError):
        svc.draft_findings(candidates)


def test_draft_findings_raises_llm_unavailable_on_invalid_json(monkeypatch):
    monkeypatch.setattr(svc, "chat", lambda **kwargs: "not valid json")

    candidates = svc.gather_finding_candidates([make_exception(id=1)], [], [], [])
    with pytest.raises(LLMUnavailableError):
        svc.draft_findings(candidates)


def test_format_candidates_for_prompt_includes_source_keys_and_facts():
    candidates = svc.gather_finding_candidates([make_exception(id=7, description="Invoice X has no PO")], [], [], [])
    text = svc._format_candidates_for_prompt(candidates)
    assert "reconciliation_exception:7" in text
    assert "Invoice X has no PO" in text
