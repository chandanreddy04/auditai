from app.models.models import ControlRuleType, DocumentType
from app.services.controls_testing_service import ControlLike, EvidenceLike, run_controls_testing


def ev(id, doc_type, ref=None, related_ref=None, amount=None, approver=None):
    return EvidenceLike(id=id, doc_type=doc_type, reference_number=ref, related_reference_number=related_ref, amount=amount, approver_name=approver)


# ------------------------------------------------- po_required_above_threshold

def test_po_required_passes_when_invoice_has_po_reference():
    control = ControlLike(id=1, rule_type=ControlRuleType.PO_REQUIRED_ABOVE_THRESHOLD, threshold_amount=1000.0)
    records = [ev(1, DocumentType.INVOICE, ref="INV-1", related_ref="PO-1", amount=1500.0)]
    results = run_controls_testing([control], records)
    assert len(results) == 1
    assert results[0].result == "pass"


def test_po_required_fails_when_invoice_has_no_po_reference():
    control = ControlLike(id=1, rule_type=ControlRuleType.PO_REQUIRED_ABOVE_THRESHOLD, threshold_amount=1000.0)
    records = [ev(1, DocumentType.INVOICE, ref="INV-1", related_ref=None, amount=1500.0)]
    results = run_controls_testing([control], records)
    assert len(results) == 1
    assert results[0].result == "fail"
    assert "INV-1" in results[0].detail


def test_po_required_does_not_apply_below_threshold():
    control = ControlLike(id=1, rule_type=ControlRuleType.PO_REQUIRED_ABOVE_THRESHOLD, threshold_amount=1000.0)
    records = [ev(1, DocumentType.INVOICE, ref="INV-1", related_ref=None, amount=500.0)]
    assert run_controls_testing([control], records) == []


def test_po_required_ignores_non_invoice_doc_types():
    control = ControlLike(id=1, rule_type=ControlRuleType.PO_REQUIRED_ABOVE_THRESHOLD, threshold_amount=1000.0)
    records = [ev(1, DocumentType.PURCHASE_ORDER, ref="PO-1", amount=5000.0)]
    assert run_controls_testing([control], records) == []


def test_po_required_boundary_amount_at_exact_threshold_applies():
    control = ControlLike(id=1, rule_type=ControlRuleType.PO_REQUIRED_ABOVE_THRESHOLD, threshold_amount=1000.0)
    records = [ev(1, DocumentType.INVOICE, ref="INV-1", related_ref=None, amount=1000.0)]
    results = run_controls_testing([control], records)
    assert len(results) == 1
    assert results[0].result == "fail"


# --------------------------------------------- approval_required_above_threshold

def test_approval_required_passes_when_approver_present():
    control = ControlLike(id=2, rule_type=ControlRuleType.APPROVAL_REQUIRED_ABOVE_THRESHOLD, threshold_amount=2000.0)
    records = [ev(1, DocumentType.INVOICE, ref="INV-2", amount=2500.0, approver="J. Smith")]
    results = run_controls_testing([control], records)
    assert len(results) == 1
    assert results[0].result == "pass"
    assert "J. Smith" in results[0].detail


def test_approval_required_fails_when_no_approver():
    control = ControlLike(id=2, rule_type=ControlRuleType.APPROVAL_REQUIRED_ABOVE_THRESHOLD, threshold_amount=2000.0)
    records = [ev(1, DocumentType.PAYMENT, ref="PMT-2", amount=2500.0, approver=None)]
    results = run_controls_testing([control], records)
    assert len(results) == 1
    assert results[0].result == "fail"


def test_approval_required_applies_to_both_invoice_and_payment():
    control = ControlLike(id=2, rule_type=ControlRuleType.APPROVAL_REQUIRED_ABOVE_THRESHOLD, threshold_amount=100.0)
    records = [
        ev(1, DocumentType.INVOICE, ref="INV-1", amount=200.0, approver=None),
        ev(2, DocumentType.PAYMENT, ref="PMT-1", amount=200.0, approver=None),
    ]
    results = run_controls_testing([control], records)
    assert len(results) == 2
    assert all(r.result == "fail" for r in results)


def test_multiple_controls_run_independently():
    controls = [
        ControlLike(id=1, rule_type=ControlRuleType.PO_REQUIRED_ABOVE_THRESHOLD, threshold_amount=1000.0),
        ControlLike(id=2, rule_type=ControlRuleType.APPROVAL_REQUIRED_ABOVE_THRESHOLD, threshold_amount=1000.0),
    ]
    records = [ev(1, DocumentType.INVOICE, ref="INV-1", related_ref=None, amount=1500.0, approver=None)]
    results = run_controls_testing(controls, records)
    assert len(results) == 2
    assert {r.control_id for r in results} == {1, 2}
    assert all(r.result == "fail" for r in results)


def test_no_records_produces_no_results():
    control = ControlLike(id=1, rule_type=ControlRuleType.PO_REQUIRED_ABOVE_THRESHOLD, threshold_amount=1000.0)
    assert run_controls_testing([control], []) == []
