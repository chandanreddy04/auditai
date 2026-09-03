from datetime import date

import pytest

from app.services import pbc_service as svc
from app.services.llm_client import LLMUnavailableError
from app.services.pbc_service import PBCLike


TODAY = date(2026, 9, 3)


def item(id=1, item_name="Bank statements", due_date=None, status="requested"):
    return PBCLike(id=id, item_name=item_name, due_date=due_date, status=status)


# -------------------------------------------------------------- is_overdue

def test_requested_item_past_due_date_is_overdue():
    assert svc.is_overdue(item(due_date=date(2026, 8, 1)), TODAY) is True


def test_requested_item_future_due_date_is_not_overdue():
    assert svc.is_overdue(item(due_date=date(2026, 12, 1)), TODAY) is False


def test_requested_item_due_today_is_not_overdue():
    assert svc.is_overdue(item(due_date=TODAY), TODAY) is False


def test_item_with_no_due_date_is_never_overdue():
    assert svc.is_overdue(item(due_date=None), TODAY) is False


def test_received_item_past_due_date_is_not_overdue():
    assert svc.is_overdue(item(due_date=date(2026, 8, 1), status="received"), TODAY) is False


def test_waived_item_past_due_date_is_not_overdue():
    assert svc.is_overdue(item(due_date=date(2026, 8, 1), status="waived"), TODAY) is False


# -------------------------------------------------------------- find_overdue

def test_find_overdue_computes_correct_days_overdue():
    items = [item(id=1, due_date=date(2026, 8, 24))]  # 10 days before TODAY
    results = svc.find_overdue(items, TODAY)
    assert len(results) == 1
    assert results[0].days_overdue == 10
    assert results[0].id == 1


def test_find_overdue_filters_out_non_overdue_items():
    items = [
        item(id=1, due_date=date(2026, 8, 1)),               # overdue
        item(id=2, due_date=date(2026, 12, 1)),               # not yet due
        item(id=3, due_date=date(2026, 8, 1), status="received"),  # closed, not overdue
        item(id=4, due_date=None),                            # no due date
    ]
    results = svc.find_overdue(items, TODAY)
    assert [r.id for r in results] == [1]


def test_find_overdue_empty_list():
    assert svc.find_overdue([], TODAY) == []


# ------------------------------------------------------- draft_reminder_email

def test_draft_reminder_email_returns_stripped_text(monkeypatch):
    monkeypatch.setattr(svc, "chat", lambda **kwargs: "  Please send the outstanding items.  \n")
    overdue = svc.find_overdue([item(id=1, due_date=date(2026, 8, 1))], TODAY)
    result = svc.draft_reminder_email("Acme Corp", "FY2026 Audit", overdue)
    assert result == "Please send the outstanding items."


def test_draft_reminder_email_propagates_llm_unavailable(monkeypatch):
    def boom(**kwargs):
        raise LLMUnavailableError("model down")
    monkeypatch.setattr(svc, "chat", boom)

    with pytest.raises(LLMUnavailableError):
        svc.draft_reminder_email("Acme Corp", "FY2026 Audit", [])


def test_format_overdue_for_prompt_includes_item_and_days():
    overdue = svc.find_overdue([item(id=1, item_name="Q1 bank statements", due_date=date(2026, 8, 20))], TODAY)
    text = svc._format_overdue_for_prompt("Acme Corp", "FY2026 Audit", overdue)
    assert "Q1 bank statements" in text
    assert "14 day(s) overdue" in text
    assert "Acme Corp" in text
