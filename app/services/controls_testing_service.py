"""
Phase 2 - the second deterministic decision engine in this app,
following the exact same discipline as reconciliation_service.py: a
human defines what a control means (a Control row), plain Python
decides whether the evidence satisfies it, and the LLM is nowhere in
this file. A "Controls Testing Agent" in the blueprint's own sense is
this service plus the human who authored the controls - not a model
call that "judges" compliance.

Two rule types for Phase 2 (see ControlRuleType in models.py):

- po_required_above_threshold: any invoice at or above the control's
  threshold must reference a purchase order. Distinct from
  reconciliation's missing_match check even though it looks similar -
  reconciliation flags ANY invoice with no PO reference, unconditionally;
  this is testing a POLICY the auditor defined ("POs are required above
  $X"), which the client may not require below that amount at all.
- approval_required_above_threshold: any invoice or payment at or above
  the threshold must show a captured approver_name.
"""

from dataclasses import dataclass

from app.models.models import ControlRuleType, DocumentType


@dataclass
class ControlLike:
    id: int
    rule_type: ControlRuleType
    threshold_amount: float


@dataclass
class EvidenceLike:
    id: int
    doc_type: DocumentType
    reference_number: str | None
    related_reference_number: str | None
    amount: float | None
    approver_name: str | None = None


@dataclass
class ControlTestResultData:
    control_id: int
    evidence_record_id: int
    result: str  # "pass" or "fail"
    detail: str


def run_controls_testing(controls: list[ControlLike], records: list[EvidenceLike]) -> list[ControlTestResultData]:
    results: list[ControlTestResultData] = []
    for control in controls:
        if control.rule_type == ControlRuleType.PO_REQUIRED_ABOVE_THRESHOLD:
            results.extend(_test_po_required(control, records))
        elif control.rule_type == ControlRuleType.APPROVAL_REQUIRED_ABOVE_THRESHOLD:
            results.extend(_test_approval_required(control, records))
    return results


def _test_po_required(control: ControlLike, records: list[EvidenceLike]) -> list[ControlTestResultData]:
    results = []
    for r in records:
        if r.doc_type != DocumentType.INVOICE or r.amount is None or r.amount < control.threshold_amount:
            continue
        if r.related_reference_number:
            results.append(ControlTestResultData(
                control.id, r.id, "pass",
                f"Invoice '{r.reference_number}' ({r.amount:,.2f}) references PO "
                f"'{r.related_reference_number}' - control satisfied.",
            ))
        else:
            results.append(ControlTestResultData(
                control.id, r.id, "fail",
                f"Invoice '{r.reference_number}' is {r.amount:,.2f}, at or above the "
                f"{control.threshold_amount:,.2f} threshold requiring a purchase order, "
                f"but no PO reference was found on the document.",
            ))
    return results


def _test_approval_required(control: ControlLike, records: list[EvidenceLike]) -> list[ControlTestResultData]:
    results = []
    for r in records:
        if r.doc_type not in (DocumentType.INVOICE, DocumentType.PAYMENT) or r.amount is None or r.amount < control.threshold_amount:
            continue
        if r.approver_name:
            results.append(ControlTestResultData(
                control.id, r.id, "pass",
                f"{r.doc_type.value} '{r.reference_number}' ({r.amount:,.2f}) was approved "
                f"by {r.approver_name} - control satisfied.",
            ))
        else:
            results.append(ControlTestResultData(
                control.id, r.id, "fail",
                f"{r.doc_type.value} '{r.reference_number}' is {r.amount:,.2f}, at or above "
                f"the {control.threshold_amount:,.2f} threshold requiring documented approval, "
                f"but no approver name was found on the document.",
            ))
    return results
