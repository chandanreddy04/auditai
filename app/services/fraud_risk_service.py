"""
A third independent deterministic engine, same discipline as
reconciliation_service.py and controls_testing_service.py: plain
Python, zero LLM calls anywhere in this file. The blueprint is explicit
that this agent "should not automatically declare fraud" - and this
project's own history backs that up hard: a reasoning-model-based
fraud/second-opinion feature in the sibling InvoiceIQ project was
built, tested, and reverted twice because it was too slow and too
unreliable for exactly this kind of judgment call. A flag from this
file is a pattern worth a human's attention, never a conclusion - it
never says "this is fraud," only "this looks unusual, please check it."

Four heuristics, matching the blueprint's own list (duplicate
payments, unusual vendors, weekend transactions, round-dollar
entries) - "sudden behavior changes" and "payments close to approval
limits" are left out of this first pass: the former needs a
historical baseline this app doesn't track yet, and the latter would
need to reach into Control thresholds from a different phase, which
is a reasonable fast-follow, not day-one scope.
"""

from dataclasses import dataclass
from datetime import date

from app.core.config import NEW_VENDOR_AMOUNT_THRESHOLD, ROUND_DOLLAR_MIN_AMOUNT
from app.models.models import DocumentType


@dataclass
class EvidenceLike:
    id: int
    doc_type: DocumentType
    vendor_name: str | None
    reference_number: str | None
    amount: float | None
    record_date: str | None  # ISO date string, as extracted


@dataclass
class FraudRiskResult:
    flag_type: str
    description: str
    evidence_record_ids: list[int]
    severity: str = "medium"


def _normalize(value: str | None) -> str | None:
    if not value:
        return None
    return "".join(value.split()).upper()


def run_fraud_risk_detection(records: list[EvidenceLike]) -> list[FraudRiskResult]:
    results: list[FraudRiskResult] = []
    results.extend(_find_duplicate_payment_risk(records))
    results.extend(_find_round_dollar_amounts(records))
    results.extend(_find_weekend_transactions(records))
    results.extend(_find_new_vendor_large_amounts(records))
    return results


def _find_duplicate_payment_risk(records: list[EvidenceLike]) -> list[FraudRiskResult]:
    """Deliberately distinct from reconciliation_service.py's duplicate
    check: that one flags the SAME reference number appearing twice.
    This one flags the same vendor billed the same amount more than
    once under DIFFERENT reference numbers - a classic split-billing /
    duplicate-payment pattern reconciliation's exact-reference-match
    can't see at all."""
    groups: dict[tuple, list[EvidenceLike]] = {}
    for r in records:
        if r.doc_type not in (DocumentType.INVOICE, DocumentType.PAYMENT) or r.amount is None or not r.vendor_name:
            continue
        key = (r.doc_type, _normalize(r.vendor_name), round(r.amount, 2))
        groups.setdefault(key, []).append(r)

    results = []
    for (doc_type, _vendor_key, amount), group in groups.items():
        distinct_refs = {_normalize(r.reference_number) for r in group if r.reference_number}
        if len(group) > 1 and len(distinct_refs) > 1:
            refs = ", ".join(sorted(r.reference_number for r in group if r.reference_number))
            results.append(FraudRiskResult(
                flag_type="duplicate_payment_risk",
                description=(
                    f"{len(group)} {doc_type.value} records for vendor '{group[0].vendor_name}' are all for "
                    f"{amount:,.2f}, under different reference numbers ({refs}) - possible duplicate or "
                    f"split billing."
                ),
                evidence_record_ids=[r.id for r in group],
                severity="high",
            ))
    return results


def _find_round_dollar_amounts(records: list[EvidenceLike]) -> list[FraudRiskResult]:
    results = []
    for r in records:
        if r.amount is None or r.amount < ROUND_DOLLAR_MIN_AMOUNT:
            continue
        if r.amount == int(r.amount) and int(r.amount) % 1000 == 0:
            results.append(FraudRiskResult(
                flag_type="round_dollar_amount",
                description=(
                    f"{r.doc_type.value} '{r.reference_number or '(no ref #)'}' is exactly {r.amount:,.2f} - "
                    f"a suspiciously round number worth confirming is an actual invoiced amount, not an estimate."
                ),
                evidence_record_ids=[r.id],
                severity="low",
            ))
    return results


def _find_weekend_transactions(records: list[EvidenceLike]) -> list[FraudRiskResult]:
    results = []
    for r in records:
        if not r.record_date:
            continue
        try:
            d = date.fromisoformat(r.record_date)
        except ValueError:
            continue
        if d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
            results.append(FraudRiskResult(
                flag_type="weekend_transaction",
                description=(
                    f"{r.doc_type.value} '{r.reference_number or '(no ref #)'}' is dated {r.record_date} "
                    f"({d.strftime('%A')}) - a weekend date, unusual for typical business activity."
                ),
                evidence_record_ids=[r.id],
                severity="medium",
            ))
    return results


def _find_new_vendor_large_amounts(records: list[EvidenceLike]) -> list[FraudRiskResult]:
    vendor_groups: dict[str, list[EvidenceLike]] = {}
    for r in records:
        if r.vendor_name:
            vendor_groups.setdefault(_normalize(r.vendor_name), []).append(r)

    results = []
    for group in vendor_groups.values():
        if len(group) != 1:
            continue
        r = group[0]
        if r.amount is not None and r.amount >= NEW_VENDOR_AMOUNT_THRESHOLD:
            results.append(FraudRiskResult(
                flag_type="new_vendor_large_amount",
                description=(
                    f"Vendor '{r.vendor_name}' appears only once in this engagement's evidence, for "
                    f"{r.amount:,.2f} - a large first-time transaction with no other activity on file for "
                    f"this vendor, worth verifying vendor legitimacy."
                ),
                evidence_record_ids=[r.id],
                severity="medium",
            ))
    return results
