"""
The actual decision-maker for Phase 1 - plain, deterministic Python,
no LLM call anywhere in this file. This is the part of "AI-based
coworker" that has to be trustworthy and explainable by construction:
every exception it raises traces back to a plain comparison a human
auditor could redo by hand and get the same answer.

What it does: given all the evidence records extracted for one
engagement, try to chain related documents together (a purchase
order -> the invoice billed against it -> the payment that settled
it) using the reference numbers the extraction step pulled out, and
flag anything that does not chain up cleanly.

Three kinds of exceptions come out of this, matching the blueprint's
own "Reconciliation Agent" example (section 8):

- missing_match: an invoice/payment that should reference an earlier
  document (because that document type exists somewhere in this
  engagement's evidence) but doesn't - either no reference number was
  found at all, or it points to a reference number that was never seen.
- amount_mismatch: two records DO chain together by reference number,
  but their amounts differ by more than RECONCILIATION_AMOUNT_TOLERANCE.
- duplicate: the same reference number shows up more than once for the
  same document type (e.g. the same invoice number submitted twice).

Nothing here ever resolves or dismisses an exception - only a human
does that, in the review queue.
"""

from dataclasses import dataclass, field

from app.core.config import RECONCILIATION_AMOUNT_TOLERANCE
from app.models.models import DocumentType

# The chain each doc type is expected to point back to, if that earlier
# type is present anywhere in the engagement's evidence at all.
_EXPECTED_PARENT = {
    DocumentType.INVOICE: DocumentType.PURCHASE_ORDER,
    DocumentType.PAYMENT: DocumentType.INVOICE,
}


@dataclass
class ExceptionResult:
    exception_type: str
    description: str
    evidence_record_ids: list[int]
    severity: str = "medium"


@dataclass
class EvidenceLike:
    """The minimal shape reconciliation needs from an evidence record -
    lets tests build plain objects instead of hitting a real database."""
    id: int
    doc_type: DocumentType
    reference_number: str | None
    related_reference_number: str | None
    amount: float | None
    vendor_name: str | None = None


def _normalize(ref: str | None) -> str | None:
    if not ref:
        return None
    return "".join(ref.split()).upper()


def run_reconciliation(records: list[EvidenceLike]) -> list[ExceptionResult]:
    exceptions: list[ExceptionResult] = []

    by_type: dict[DocumentType, list[EvidenceLike]] = {}
    for r in records:
        by_type.setdefault(r.doc_type, []).append(r)

    exceptions.extend(_find_duplicates(by_type))
    exceptions.extend(_find_chain_exceptions(by_type))

    return exceptions


def _find_duplicates(by_type: dict[DocumentType, list["EvidenceLike"]]) -> list[ExceptionResult]:
    exceptions = []
    for doc_type, group in by_type.items():
        seen: dict[str, list[EvidenceLike]] = {}
        for r in group:
            ref = _normalize(r.reference_number)
            if not ref:
                continue
            seen.setdefault(ref, []).append(r)
        for ref, dupes in seen.items():
            if len(dupes) > 1:
                exceptions.append(ExceptionResult(
                    exception_type="duplicate",
                    description=(
                        f"{len(dupes)} {doc_type.value} records share the same "
                        f"reference number '{dupes[0].reference_number}' - possible "
                        f"duplicate submission."
                    ),
                    evidence_record_ids=[d.id for d in dupes],
                    severity="high",
                ))
    return exceptions


def _find_chain_exceptions(by_type: dict[DocumentType, list["EvidenceLike"]]) -> list[ExceptionResult]:
    exceptions = []

    for doc_type, parent_type in _EXPECTED_PARENT.items():
        children = by_type.get(doc_type, [])
        if not children:
            continue
        parents = by_type.get(parent_type, [])
        if not parents:
            # This document type doesn't exist anywhere in the engagement's
            # evidence at all - nothing to chain against, so no exception.
            # (e.g. an engagement where no POs were ever uploaded.)
            continue
        parent_by_ref = {_normalize(p.reference_number): p for p in parents if p.reference_number}

        for child in children:
            related_ref = _normalize(child.related_reference_number)
            if not related_ref:
                exceptions.append(ExceptionResult(
                    exception_type="missing_match",
                    description=(
                        f"{doc_type.value} '{child.reference_number or '(no ref #)'}' "
                        f"does not reference a {parent_type.value} number, but "
                        f"{parent_type.value} records exist in this engagement."
                    ),
                    evidence_record_ids=[child.id],
                    severity="medium",
                ))
                continue

            parent = parent_by_ref.get(related_ref)
            if parent is None:
                exceptions.append(ExceptionResult(
                    exception_type="missing_match",
                    description=(
                        f"{doc_type.value} '{child.reference_number or '(no ref #)'}' "
                        f"references {parent_type.value} '{child.related_reference_number}', "
                        f"but no such {parent_type.value} was found in this engagement's evidence."
                    ),
                    evidence_record_ids=[child.id],
                    severity="medium",
                ))
                continue

            if child.amount is not None and parent.amount is not None:
                diff = abs(child.amount - parent.amount)
                if diff > RECONCILIATION_AMOUNT_TOLERANCE:
                    exceptions.append(ExceptionResult(
                        exception_type="amount_mismatch",
                        description=(
                            f"{doc_type.value} '{child.reference_number}' is "
                            f"{child.amount:,.2f}, but the {parent_type.value} it "
                            f"references ('{parent.reference_number}') is "
                            f"{parent.amount:,.2f} - a difference of {diff:,.2f}."
                        ),
                        evidence_record_ids=[child.id, parent.id],
                        severity="high",
                    ))

    return exceptions
