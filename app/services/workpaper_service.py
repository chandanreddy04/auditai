"""
Phase 3 - drafting, not deciding. Every fact in a workpaper draft was
already decided by a human or by the deterministic engines in
reconciliation_service.py / controls_testing_service.py; this file's
only job is turning those already-settled facts into readable prose
for a human auditor to edit and sign off on. The LLM is never shown
raw evidence and never asked to draw a conclusion - it's handed a
finished summary and asked to write it up, the same "narrate, don't
decide" boundary used everywhere else in this app.

Split into two layers on purpose, matching the pattern already used in
the other two engines:
- build_engagement_summary() / _summarize(): plain Python, zero LLM,
  fully unit-testable with constructed objects instead of a real DB.
- draft_workpaper_narrative(): the one LLM call, schema-free (this is
  prose, not structured extraction) - takes a summary in, returns text.
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.models import (
    Control, ControlTestResult, Document, Engagement, EvidenceRecord,
    ExceptionStatus, ReconciliationException,
)
from app.services.llm_client import LLMUnavailableError, chat

logger = logging.getLogger(__name__)


@dataclass
class ExceptionDetail:
    exception_type: str
    description: str
    status: str
    resolved_by: str | None = None
    resolution_note: str | None = None


@dataclass
class ControlFindingDetail:
    control_name: str
    result: str
    status: str
    detail: str
    resolved_by: str | None = None
    resolution_note: str | None = None


@dataclass
class EngagementSummary:
    engagement_name: str
    client_name: str
    audit_type: str

    documents_total: int = 0
    documents_failed: int = 0
    documents_by_type: dict = field(default_factory=dict)

    evidence_total: int = 0
    evidence_by_type: dict = field(default_factory=dict)
    evidence_amount_by_type: dict = field(default_factory=dict)

    exceptions_total: int = 0
    exceptions_open: int = 0
    exceptions_resolved: int = 0
    exceptions_dismissed: int = 0
    exception_details: list = field(default_factory=list)  # list[ExceptionDetail]

    controls_defined: int = 0
    control_results_pass: int = 0
    control_results_fail_open: int = 0
    control_results_fail_closed: int = 0
    control_findings: list = field(default_factory=list)  # list[ControlFindingDetail]


def _summarize(
    engagement: Engagement,
    documents: list[Document],
    evidence_records: list[EvidenceRecord],
    exceptions: list[ReconciliationException],
    control_results: list[ControlTestResult],
) -> EngagementSummary:
    summary = EngagementSummary(
        engagement_name=engagement.name,
        client_name=engagement.client.name,
        audit_type=engagement.audit_type,
    )

    summary.documents_total = len(documents)
    summary.documents_failed = sum(1 for d in documents if d.status.value == "failed")
    for d in documents:
        summary.documents_by_type[d.doc_type.value] = summary.documents_by_type.get(d.doc_type.value, 0) + 1

    summary.evidence_total = len(evidence_records)
    for r in evidence_records:
        key = r.doc_type.value
        summary.evidence_by_type[key] = summary.evidence_by_type.get(key, 0) + 1
        if r.amount is not None:
            summary.evidence_amount_by_type[key] = summary.evidence_amount_by_type.get(key, 0.0) + r.amount

    summary.exceptions_total = len(exceptions)
    for e in exceptions:
        status = e.status.value
        if status == "open":
            summary.exceptions_open += 1
        elif status == "resolved":
            summary.exceptions_resolved += 1
        elif status == "dismissed":
            summary.exceptions_dismissed += 1
        summary.exception_details.append(ExceptionDetail(
            exception_type=e.exception_type.value, description=e.description, status=status,
            resolved_by=e.resolved_by, resolution_note=e.resolution_note,
        ))

    summary.controls_defined = len({c.control_id for c in control_results}) or len(control_results)
    for r in control_results:
        if r.result.value == "pass":
            summary.control_results_pass += 1
        elif r.status.value == "open":
            summary.control_results_fail_open += 1
        else:
            summary.control_results_fail_closed += 1
        summary.control_findings.append(ControlFindingDetail(
            control_name=r.control.name, result=r.result.value, status=r.status.value, detail=r.detail,
            resolved_by=r.resolved_by, resolution_note=r.resolution_note,
        ))

    return summary


def build_engagement_summary(db: Session, engagement: Engagement) -> EngagementSummary:
    documents = db.query(Document).filter(Document.engagement_id == engagement.id).all()
    evidence_records = db.query(EvidenceRecord).filter(EvidenceRecord.engagement_id == engagement.id).all()
    exceptions = db.query(ReconciliationException).filter(ReconciliationException.engagement_id == engagement.id).all()
    control_results = db.query(ControlTestResult).filter(ControlTestResult.engagement_id == engagement.id).all()
    return _summarize(engagement, documents, evidence_records, exceptions, control_results)


SYSTEM_PROMPT = (
    "You are drafting an internal audit workpaper summary memo. You are "
    "given a structured summary of one engagement's facts - counts of "
    "documents and evidence reviewed, reconciliation exceptions and how "
    "each was resolved, and control test results. Write a professional, "
    "concise memo (4-8 short paragraphs) covering: (1) scope of the "
    "evidence reviewed, (2) reconciliation results including any open "
    "or resolved exceptions and their disposition, (3) controls tested "
    "and their results, (4) a closing note on any items still open and "
    "requiring follow-up. \n\n"
    "Use ONLY the facts given below. Never invent a number, a name, or "
    "a finding that is not in the summary. If a section has nothing to "
    "report (e.g. no exceptions), say so plainly rather than omitting "
    "it. Write in plain professional prose, not bullet points.\n\n"
    "Output ONLY the body paragraphs - begin directly with the first "
    "substantive sentence. Do NOT format this as a letter or email: no "
    "'To:'/'From:'/'Subject:' header, no salutation ('Dear...'), and no "
    "closing/signature block ('Best regards', '[Your Name]', etc.)."
)


def _format_summary_for_prompt(summary: EngagementSummary) -> str:
    lines = [
        f"Engagement: {summary.engagement_name} (client: {summary.client_name}, audit type: {summary.audit_type})",
        f"Documents reviewed: {summary.documents_total} total, {summary.documents_failed} failed to process, "
        f"by type: {summary.documents_by_type}",
        f"Evidence records extracted: {summary.evidence_total}, by type: {summary.evidence_by_type}, "
        f"total amount by type: {summary.evidence_amount_by_type}",
        f"Reconciliation exceptions: {summary.exceptions_total} total "
        f"({summary.exceptions_open} open, {summary.exceptions_resolved} resolved, "
        f"{summary.exceptions_dismissed} dismissed).",
    ]
    for e in summary.exception_details:
        note = f" Resolution: {e.resolved_by} - {e.resolution_note}" if e.resolution_note else ""
        lines.append(f"  - [{e.status}] {e.exception_type}: {e.description}{note}")

    lines.append(
        f"Control test results: {summary.control_results_pass} passed, "
        f"{summary.control_results_fail_open} failed and still open, "
        f"{summary.control_results_fail_closed} failed and closed."
    )
    for c in summary.control_findings:
        note = f" Resolution: {c.resolved_by} - {c.resolution_note}" if c.resolution_note else ""
        lines.append(f"  - [{c.control_name} / {c.result}/{c.status}] {c.detail}{note}")

    return "\n".join(lines)


def draft_workpaper_narrative(summary: EngagementSummary) -> str:
    try:
        return chat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _format_summary_for_prompt(summary)},
            ],
        ).strip()
    except LLMUnavailableError as e:
        logger.warning("Workpaper drafting failed: %s", e)
        raise
