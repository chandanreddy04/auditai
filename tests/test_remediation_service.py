from datetime import date

import pytest

from app.services import remediation_service as svc
from app.services.llm_client import LLMUnavailableError
from app.services.remediation_service import FindingLike


TODAY = date(2026, 9, 3)


def finding(id=1, title="Missing PO for high-value invoice", owner=None, target_date=None, status="open"):
    return FindingLike(id=id, title=title, owner=owner, target_date=target_date, status=status)


# -------------------------------------------------------------- is_overdue

def test_open_finding_past_target_date_is_overdue():
    assert svc.is_overdue(finding(target_date=date(2026, 8, 1), owner="Jane"), TODAY) is True


def test_open_finding_future_target_date_is_not_overdue():
    assert svc.is_overdue(finding(target_date=date(2026, 12, 1), owner="Jane"), TODAY) is False


def test_open_finding_due_today_is_not_overdue():
    assert svc.is_overdue(finding(target_date=TODAY, owner="Jane"), TODAY) is False


def test_finding_with_no_target_date_is_never_overdue():
    assert svc.is_overdue(finding(target_date=None, owner="Jane"), TODAY) is False


def test_resolved_finding_past_target_date_is_not_overdue():
    assert svc.is_overdue(finding(target_date=date(2026, 8, 1), owner="Jane", status="resolved"), TODAY) is False


def test_dismissed_finding_past_target_date_is_not_overdue():
    assert svc.is_overdue(finding(target_date=date(2026, 8, 1), owner="Jane", status="dismissed"), TODAY) is False


def test_finding_with_target_date_but_no_owner_is_still_overdue():
    # Unassigned owner doesn't exempt a finding from its own deadline -
    # only a missing target_date does (see module docstring).
    assert svc.is_overdue(finding(target_date=date(2026, 8, 1), owner=None), TODAY) is True


# -------------------------------------------------------------- find_overdue

def test_find_overdue_computes_correct_days_overdue():
    findings = [finding(id=1, target_date=date(2026, 8, 24), owner="Jane")]  # 10 days before TODAY
    results = svc.find_overdue(findings, TODAY)
    assert len(results) == 1
    assert results[0].days_overdue == 10
    assert results[0].id == 1


def test_find_overdue_filters_out_non_overdue_findings():
    findings = [
        finding(id=1, target_date=date(2026, 8, 1), owner="Jane"),                    # overdue
        finding(id=2, target_date=date(2026, 12, 1), owner="Jane"),                   # not yet due
        finding(id=3, target_date=date(2026, 8, 1), owner="Jane", status="resolved"),  # closed
        finding(id=4, target_date=None, owner="Jane"),                                # no target date
    ]
    results = svc.find_overdue(findings, TODAY)
    assert [r.id for r in results] == [1]


def test_find_overdue_empty_list():
    assert svc.find_overdue([], TODAY) == []


# --------------------------------------------------- draft_remediation_reminder

def test_draft_remediation_reminder_returns_stripped_text(monkeypatch):
    monkeypatch.setattr(svc, "chat", lambda **kwargs: "  Please provide a remediation update.  \n")
    overdue = svc.find_overdue([finding(id=1, target_date=date(2026, 8, 1), owner="Jane")], TODAY)
    result = svc.draft_remediation_reminder("Acme Corp", "FY2026 Audit", overdue)
    assert result == "Please provide a remediation update."


def test_draft_remediation_reminder_propagates_llm_unavailable(monkeypatch):
    def boom(**kwargs):
        raise LLMUnavailableError("model down")
    monkeypatch.setattr(svc, "chat", boom)

    with pytest.raises(LLMUnavailableError):
        svc.draft_remediation_reminder("Acme Corp", "FY2026 Audit", [])


def test_format_overdue_for_prompt_includes_title_owner_and_days():
    overdue = svc.find_overdue(
        [finding(id=1, title="Unapproved vendor payment", target_date=date(2026, 8, 20), owner="Jane Smith")], TODAY,
    )
    text = svc._format_overdue_for_prompt("Acme Corp", "FY2026 Audit", overdue)
    assert "Unapproved vendor payment" in text
    assert "Jane Smith" in text
    assert "14 day(s) overdue" in text
    assert "Acme Corp" in text


def test_format_overdue_for_prompt_handles_unassigned_owner():
    overdue = svc.find_overdue([finding(id=1, target_date=date(2026, 8, 1), owner=None)], TODAY)
    text = svc._format_overdue_for_prompt("Acme Corp", "FY2026 Audit", overdue)
    assert "(no owner assigned)" in text
