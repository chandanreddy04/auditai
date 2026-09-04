from app.models.models import DocumentType
from app.services.fraud_risk_service import EvidenceLike, run_fraud_risk_detection


def ev(id, doc_type=DocumentType.INVOICE, vendor=None, ref=None, amount=None, record_date=None):
    return EvidenceLike(id=id, doc_type=doc_type, vendor_name=vendor, reference_number=ref, amount=amount, record_date=record_date)


# --------------------------------------------------- duplicate_payment_risk

def test_duplicate_payment_risk_flags_same_vendor_amount_different_refs():
    records = [
        ev(1, vendor="Acme Corp", ref="INV-1", amount=5000.0),
        ev(2, vendor="Acme Corp", ref="INV-2", amount=5000.0),
    ]
    results = run_fraud_risk_detection(records)
    flags = [r for r in results if r.flag_type == "duplicate_payment_risk"]
    assert len(flags) == 1
    assert set(flags[0].evidence_record_ids) == {1, 2}
    assert flags[0].severity == "high"


def test_duplicate_payment_risk_ignores_same_reference_number():
    # Same ref number is reconciliation_service's job (exact duplicate),
    # not this heuristic's - this only fires on DIFFERENT ref numbers.
    records = [
        ev(1, vendor="Acme Corp", ref="INV-1", amount=5000.0),
        ev(2, vendor="Acme Corp", ref="INV-1", amount=5000.0),
    ]
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "duplicate_payment_risk"]


def test_duplicate_payment_risk_ignores_different_amounts():
    records = [
        ev(1, vendor="Acme Corp", ref="INV-1", amount=5000.0),
        ev(2, vendor="Acme Corp", ref="INV-2", amount=6000.0),
    ]
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "duplicate_payment_risk"]


def test_duplicate_payment_risk_ignores_different_vendors():
    records = [
        ev(1, vendor="Acme Corp", ref="INV-1", amount=5000.0),
        ev(2, vendor="Beta LLC", ref="INV-2", amount=5000.0),
    ]
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "duplicate_payment_risk"]


def test_duplicate_payment_risk_vendor_name_case_and_whitespace_insensitive():
    records = [
        ev(1, vendor="Acme Corp", ref="INV-1", amount=5000.0),
        ev(2, vendor="  acme   corp ", ref="INV-2", amount=5000.0),
    ]
    results = run_fraud_risk_detection(records)
    assert len([r for r in results if r.flag_type == "duplicate_payment_risk"]) == 1


def test_duplicate_payment_risk_ignores_purchase_orders():
    records = [
        ev(1, doc_type=DocumentType.PURCHASE_ORDER, vendor="Acme Corp", ref="PO-1", amount=5000.0),
        ev(2, doc_type=DocumentType.PURCHASE_ORDER, vendor="Acme Corp", ref="PO-2", amount=5000.0),
    ]
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "duplicate_payment_risk"]


# ------------------------------------------------------- round_dollar_amount

def test_round_dollar_amount_flags_exact_thousand():
    records = [ev(1, amount=5000.0)]
    results = run_fraud_risk_detection(records)
    flags = [r for r in results if r.flag_type == "round_dollar_amount"]
    assert len(flags) == 1
    assert flags[0].severity == "low"


def test_round_dollar_amount_ignores_amount_with_cents():
    records = [ev(1, amount=5000.50)]
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "round_dollar_amount"]


def test_round_dollar_amount_ignores_non_thousand_round_number():
    records = [ev(1, amount=5500.0)]
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "round_dollar_amount"]


def test_round_dollar_amount_ignores_amount_below_minimum():
    records = [ev(1, amount=20.0)]  # round, but too small to be worth flagging
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "round_dollar_amount"]


# ------------------------------------------------------- weekend_transaction

def test_weekend_transaction_flags_saturday():
    records = [ev(1, record_date="2026-09-05")]  # a Saturday
    results = run_fraud_risk_detection(records)
    flags = [r for r in results if r.flag_type == "weekend_transaction"]
    assert len(flags) == 1
    assert "Saturday" in flags[0].description


def test_weekend_transaction_flags_sunday():
    records = [ev(1, record_date="2026-09-06")]  # a Sunday
    results = run_fraud_risk_detection(records)
    assert len([r for r in results if r.flag_type == "weekend_transaction"]) == 1


def test_weekend_transaction_ignores_weekday():
    records = [ev(1, record_date="2026-09-04")]  # a Friday
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "weekend_transaction"]


def test_weekend_transaction_ignores_missing_date():
    records = [ev(1, record_date=None)]
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "weekend_transaction"]


def test_weekend_transaction_ignores_malformed_date():
    records = [ev(1, record_date="not-a-date")]
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "weekend_transaction"]


# --------------------------------------------------- new_vendor_large_amount

def test_new_vendor_large_amount_flags_single_appearance_above_threshold():
    records = [ev(1, vendor="Brand New Vendor Inc", amount=10000.0)]
    results = run_fraud_risk_detection(records)
    flags = [r for r in results if r.flag_type == "new_vendor_large_amount"]
    assert len(flags) == 1
    assert flags[0].severity == "medium"


def test_new_vendor_large_amount_ignores_below_threshold():
    records = [ev(1, vendor="Brand New Vendor Inc", amount=100.0)]
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "new_vendor_large_amount"]


def test_new_vendor_large_amount_ignores_repeat_vendor():
    records = [
        ev(1, vendor="Regular Vendor", amount=10000.0),
        ev(2, vendor="Regular Vendor", amount=200.0),
    ]
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "new_vendor_large_amount"]


def test_new_vendor_large_amount_ignores_missing_vendor_name():
    records = [ev(1, vendor=None, amount=10000.0)]
    results = run_fraud_risk_detection(records)
    assert not [r for r in results if r.flag_type == "new_vendor_large_amount"]


# ----------------------------------------------------------------- combined

def test_no_records_produces_no_results():
    assert run_fraud_risk_detection([]) == []


def test_clean_evidence_produces_no_flags():
    records = [
        ev(1, doc_type=DocumentType.PURCHASE_ORDER, vendor="Regular Vendor", ref="PO-1", amount=1234.56, record_date="2026-09-04"),
        ev(2, doc_type=DocumentType.INVOICE, vendor="Regular Vendor", ref="INV-1", amount=1234.56, record_date="2026-09-04"),
    ]
    assert run_fraud_risk_detection(records) == []


def test_multiple_heuristics_can_fire_on_the_same_record():
    # A single record can trip more than one heuristic at once - each
    # is an independent lens on the same evidence, not mutually exclusive.
    records = [ev(1, vendor="Brand New Vendor Inc", amount=5000.0, record_date="2026-09-05")]  # round + new vendor + weekend
    results = run_fraud_risk_detection(records)
    flag_types = {r.flag_type for r in results}
    assert "round_dollar_amount" in flag_types
    assert "new_vendor_large_amount" in flag_types
    assert "weekend_transaction" in flag_types
