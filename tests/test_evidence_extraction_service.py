import json

import pytest

from app.services import evidence_extraction_service as svc
from app.services.llm_client import LLMUnavailableError


def test_extract_evidence_success(monkeypatch):
    payload = {
        "doc_type": "invoice", "vendor_name": "Acme Supplies", "reference_number": "INV-42",
        "related_reference_number": "PO-9", "amount": 250.0, "currency": "USD", "record_date": "2026-01-15",
    }
    monkeypatch.setattr(svc, "chat", lambda **kwargs: json.dumps(payload))

    result = svc.extract_evidence("some invoice text")
    assert result.doc_type == "invoice"
    assert result.vendor_name == "Acme Supplies"
    assert result.related_reference_number == "PO-9"


def test_extract_evidence_propagates_llm_unavailable(monkeypatch):
    def boom(**kwargs):
        raise LLMUnavailableError("model down")
    monkeypatch.setattr(svc, "chat", boom)

    with pytest.raises(LLMUnavailableError):
        svc.extract_evidence("some text")


def test_extract_evidence_invalid_json_raises_llm_unavailable(monkeypatch):
    monkeypatch.setattr(svc, "chat", lambda **kwargs: "not json")

    with pytest.raises(LLMUnavailableError):
        svc.extract_evidence("some text")


# Live-found bugs against a real local model (phi3.5 via Ollama), fixed
# in this service - regression coverage so they can't silently come back.

@pytest.mark.parametrize("raw,expected", [
    ("purchase_order", "purchase_order"),
    ("Purchase Order", "purchase_order"),   # human-readable label instead of the schema value
    ("PURCHASE ORDER", "purchase_order"),
    ("po", "purchase_order"),
    ("Invoice", "invoice"),
    ("Bank Statement", "bank_statement"),
    ("Remittance Advice", "payment"),        # a real synonym the model used for a payment record
    ("something the model made up", "unknown"),
])
def test_doc_type_normalization(raw, expected):
    assert svc._normalize_doc_type(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("USD", "USD"),
    ("usd", "USD"),
    ("unknown", "USD"),   # the model wrote the literal word "unknown" instead of a currency code
    ("", "USD"),
    (None, "USD"),
    ("EUR", "EUR"),
])
def test_currency_normalization(raw, expected):
    assert svc._normalize_currency(raw) == expected


def test_extract_evidence_normalizes_doc_type_and_currency_from_raw_llm_output(monkeypatch):
    # Reproduces the exact live failure: the model returned a human-readable
    # doc_type label and wrote "unknown" into currency instead of a code.
    payload = {
        "doc_type": "Purchase Order", "vendor_name": "Northwind Office Supplies",
        "reference_number": "PO-100", "related_reference_number": None,
        "amount": 5000.0, "currency": "unknown", "record_date": "2026-08-01",
    }
    monkeypatch.setattr(svc, "chat", lambda **kwargs: json.dumps(payload))

    result = svc.extract_evidence("some PO text")
    assert result.doc_type == "purchase_order"
    assert result.currency == "USD"
