from app.models.models import DocumentType
from app.services.reconciliation_service import EvidenceLike, run_reconciliation


def ev(id, doc_type, ref=None, related_ref=None, amount=None):
    return EvidenceLike(id=id, doc_type=doc_type, reference_number=ref, related_reference_number=related_ref, amount=amount)


def test_clean_chain_produces_no_exceptions():
    records = [
        ev(1, DocumentType.PURCHASE_ORDER, ref="PO-100", amount=500.0),
        ev(2, DocumentType.INVOICE, ref="INV-1", related_ref="PO-100", amount=500.0),
        ev(3, DocumentType.PAYMENT, ref="PMT-1", related_ref="INV-1", amount=500.0),
    ]
    assert run_reconciliation(records) == []


def test_invoice_with_no_po_reference_when_pos_exist():
    records = [
        ev(1, DocumentType.PURCHASE_ORDER, ref="PO-100", amount=500.0),
        ev(2, DocumentType.INVOICE, ref="INV-1", related_ref=None, amount=500.0),
    ]
    results = run_reconciliation(records)
    assert len(results) == 1
    assert results[0].exception_type == "missing_match"
    assert results[0].evidence_record_ids == [2]


def test_invoice_referencing_a_po_that_does_not_exist():
    records = [
        ev(1, DocumentType.PURCHASE_ORDER, ref="PO-100", amount=500.0),
        ev(2, DocumentType.INVOICE, ref="INV-1", related_ref="PO-999", amount=500.0),
    ]
    results = run_reconciliation(records)
    assert len(results) == 1
    assert results[0].exception_type == "missing_match"
    assert "PO-999" in results[0].description


def test_no_pos_in_engagement_at_all_produces_no_missing_match_noise():
    # No purchase orders anywhere in this engagement's evidence - an
    # invoice with no PO reference should NOT be flagged, since there's
    # nothing for it to chain against in the first place.
    records = [ev(1, DocumentType.INVOICE, ref="INV-1", related_ref=None, amount=500.0)]
    assert run_reconciliation(records) == []


def test_amount_mismatch_beyond_tolerance():
    records = [
        ev(1, DocumentType.PURCHASE_ORDER, ref="PO-100", amount=500.0),
        ev(2, DocumentType.INVOICE, ref="INV-1", related_ref="PO-100", amount=550.0),
    ]
    results = run_reconciliation(records)
    assert len(results) == 1
    assert results[0].exception_type == "amount_mismatch"
    assert set(results[0].evidence_record_ids) == {1, 2}


def test_amount_within_tolerance_is_not_flagged():
    records = [
        ev(1, DocumentType.PURCHASE_ORDER, ref="PO-100", amount=500.00),
        ev(2, DocumentType.INVOICE, ref="INV-1", related_ref="PO-100", amount=500.50),
    ]
    assert run_reconciliation(records) == []


def test_duplicate_reference_number_same_doc_type():
    records = [
        ev(1, DocumentType.INVOICE, ref="INV-1", amount=100.0),
        ev(2, DocumentType.INVOICE, ref="inv-1", amount=100.0),  # case/whitespace-insensitive match
    ]
    results = run_reconciliation(records)
    dup = [r for r in results if r.exception_type == "duplicate"]
    assert len(dup) == 1
    assert set(dup[0].evidence_record_ids) == {1, 2}


def test_reference_matching_is_case_and_whitespace_insensitive():
    records = [
        ev(1, DocumentType.PURCHASE_ORDER, ref=" po-100 ", amount=500.0),
        ev(2, DocumentType.INVOICE, ref="INV-1", related_ref="PO-100", amount=500.0),
    ]
    assert run_reconciliation(records) == []
