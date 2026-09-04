"""
Phase 5 - coordination, not new judgment. Every agent this file calls
already existed and already made its own deterministic or narrow-LLM
decisions elsewhere (evidence extraction, reconciliation, fraud-risk
detection, controls testing); this file's only job is calling them in
the right order and writing down, step by step, that it did.

This is the blueprint's own architecture diagram - Human Auditor ->
Orchestrator -> testing agents -> Human Review - made real as code
instead of staying a diagram. Before this phase, routes.py called
these three functions directly, one after another, with no record of
having done so beyond the audit log's prose lines. Now there is a
persisted OrchestrationRun with ordered OrchestrationStep rows: which
agent ran, in what order, with what outcome, how long it took - the
full intended pipeline, including the steps that were correctly
SKIPPED, not just the ones that happened to execute.

Deliberately excludes workpaper drafting and PBC reminders from this
pipeline. Both stay human-triggered "when I'm ready" actions (see
Phase 3/4's own README notes) - automatically regenerating a workpaper
after every single upload would silently overwrite an auditor's
in-progress edits and burn a slow LLM call for no reason. Orchestration
means running the RIGHT agents at the RIGHT time, not running
everything all the time.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.models.models import (
    Control, ControlTestResult, ControlTestStatus, Document, DocumentStatus, DocumentType,
    Engagement, EvidenceRecord, ExceptionStatus, FraudRiskFlag, OrchestrationRun, OrchestrationRunStatus,
    OrchestrationStep, OrchestrationStepStatus, OrchestrationTrigger, ReconciliationException,
)
from app.services import audit_log_service
from app.services import controls_testing_service as controls_svc
from app.services import fraud_risk_service
from app.services.evidence_extraction_service import extract_evidence, extract_evidence_from_image
from app.services.llm_client import LLMUnavailableError
from app.services.pdf_text_service import extract_text, has_extractable_text, render_pdf_page_to_image
from app.services.reconciliation_service import EvidenceLike, run_reconciliation

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AgentStepOutcome:
    status: OrchestrationStepStatus
    detail: str


# ------------------------------------------------------------------ agents
# Each function below is one named agent - the exact same logic that
# used to live inline in routes.py, relocated here so it can be called
# either as one step in an orchestrated run, or standalone (a control
# created after evidence already exists only needs the controls-testing
# agent to re-run, not a whole document pipeline).

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")


def run_evidence_extraction_agent(db: Session, document: Document, engagement: Engagement) -> AgentStepOutcome:
    """Three ways in, one way out. A directly-uploaded photo/screenshot
    (JPG/PNG) always goes straight to the vision model. A PDF tries its
    real text layer first (cheap, fast, no vision model needed); only
    if that PDF has no usable text at all (a scanned page) does it fall
    back to rendering the page as an image and using the vision model -
    same two-step gate + fallback already proven out in InvoiceIQ. A
    photo that's too blurry/unreadable for even the vision model to
    make sense of still comes back as a FAILED step, same as any other
    extraction failure - vision isn't magic, it's just another reader."""
    filename = (document.filename or "").lower()

    if filename.endswith(_IMAGE_EXTENSIONS):
        try:
            with open(document.file_path, "rb") as f:
                image_bytes = f.read()
        except Exception as e:
            logger.warning("Could not read image file for document %s: %s", document.id, e)
            document.status = DocumentStatus.FAILED
            document.failure_reason = "Could not read this file as an image."
            db.commit()
            return AgentStepOutcome(OrchestrationStepStatus.FAILED, document.failure_reason)
        return _extract_from_image(db, document, engagement, image_bytes)

    try:
        text = extract_text(document.file_path)
    except Exception as e:
        logger.warning("PDF text extraction failed for document %s: %s", document.id, e)
        document.status = DocumentStatus.FAILED
        document.failure_reason = "Could not read this file as a PDF."
        db.commit()
        return AgentStepOutcome(OrchestrationStepStatus.FAILED, document.failure_reason)

    if has_extractable_text(text):
        document.raw_text = text
        db.commit()
        return _extract_from_text(db, document, engagement, text)

    # No text layer - render the page as an image and hand it to the
    # vision model instead of giving up, same fallback InvoiceIQ proved out.
    try:
        image_bytes = render_pdf_page_to_image(document.file_path)
    except Exception as e:
        logger.warning("Could not render PDF page to image for document %s: %s", document.id, e)
        document.status = DocumentStatus.FAILED
        document.failure_reason = "No text layer found, and could not render the page as an image either."
        db.commit()
        return AgentStepOutcome(OrchestrationStepStatus.FAILED, document.failure_reason)
    return _extract_from_image(db, document, engagement, image_bytes)


def _extract_from_text(db: Session, document: Document, engagement: Engagement, text: str) -> AgentStepOutcome:
    try:
        extracted = extract_evidence(text)
    except LLMUnavailableError as e:
        return _fail_extraction(db, document, engagement, f"AI extraction unavailable: {e}")
    return _save_extracted_evidence(db, document, engagement, extracted)


def _extract_from_image(db: Session, document: Document, engagement: Engagement, image_bytes: bytes) -> AgentStepOutcome:
    try:
        extracted = extract_evidence_from_image(image_bytes)
    except LLMUnavailableError as e:
        return _fail_extraction(db, document, engagement, f"AI vision extraction unavailable: {e}")
    return _save_extracted_evidence(db, document, engagement, extracted)


def _fail_extraction(db: Session, document: Document, engagement: Engagement, reason: str) -> AgentStepOutcome:
    document.status = DocumentStatus.FAILED
    document.failure_reason = reason
    db.commit()
    audit_log_service.log(
        db, actor="evidence_extraction_agent", action="extraction_failed", detail=reason,
        engagement_id=engagement.id, client_id=engagement.client_id,
    )
    return AgentStepOutcome(OrchestrationStepStatus.FAILED, reason)


def _save_extracted_evidence(db: Session, document: Document, engagement: Engagement, extracted) -> AgentStepOutcome:
    try:
        doc_type = DocumentType(extracted.doc_type)
    except ValueError:
        doc_type = DocumentType.UNKNOWN

    document.doc_type = doc_type
    document.status = DocumentStatus.EXTRACTED
    db.commit()

    record = EvidenceRecord(
        document_id=document.id, engagement_id=engagement.id, client_id=engagement.client_id,
        doc_type=doc_type, vendor_name=extracted.vendor_name,
        reference_number=extracted.reference_number, related_reference_number=extracted.related_reference_number,
        amount=extracted.amount, currency=extracted.currency, record_date=extracted.record_date,
        approver_name=extracted.approver_name,
    )
    db.add(record)
    db.commit()

    detail = f"{doc_type.value} ref={extracted.reference_number} amount={extracted.amount}"
    audit_log_service.log(
        db, actor="evidence_extraction_agent", action="evidence_extracted", detail=detail,
        engagement_id=engagement.id, client_id=engagement.client_id,
    )
    return AgentStepOutcome(OrchestrationStepStatus.SUCCESS, detail)


def run_reconciliation_agent(db: Session, engagement: Engagement) -> AgentStepOutcome:
    records = db.query(EvidenceRecord).filter(EvidenceRecord.engagement_id == engagement.id).all()
    if not records:
        return AgentStepOutcome(OrchestrationStepStatus.SKIPPED, "No evidence records to reconcile yet.")

    evidence_like = [
        EvidenceLike(
            id=r.id, doc_type=r.doc_type, reference_number=r.reference_number,
            related_reference_number=r.related_reference_number, amount=r.amount, vendor_name=r.vendor_name,
        )
        for r in records
    ]
    results = run_reconciliation(evidence_like)

    existing_open = (
        db.query(ReconciliationException)
        .filter(ReconciliationException.engagement_id == engagement.id, ReconciliationException.status == ExceptionStatus.OPEN)
        .all()
    )
    existing_keys = {(e.exception_type.value, e.evidence_record_ids) for e in existing_open}

    new_count = 0
    for result in results:
        ids_str = ",".join(str(i) for i in sorted(result.evidence_record_ids))
        key = (result.exception_type, ids_str)
        if key in existing_keys:
            continue
        db.add(ReconciliationException(
            engagement_id=engagement.id, client_id=engagement.client_id,
            exception_type=result.exception_type, description=result.description,
            evidence_record_ids=ids_str, severity=result.severity,
        ))
        new_count += 1
    db.commit()

    detail = f"{len(results)} exceptions found, {new_count} new"
    audit_log_service.log(
        db, actor="reconciliation_agent", action="reconciliation_run", detail=detail,
        engagement_id=engagement.id, client_id=engagement.client_id,
    )
    return AgentStepOutcome(OrchestrationStepStatus.SUCCESS, detail)


def run_fraud_risk_agent(db: Session, engagement: Engagement) -> AgentStepOutcome:
    """Deterministic pattern-matching only - see fraud_risk_service.py's
    own module docstring for why this deliberately never calls an LLM.
    Same re-run-everything-every-time + dedup-by-key approach as
    reconciliation and controls testing: a flag is only ever written
    OPEN the first time a given (flag_type, evidence records) pair
    appears, so re-running never duplicates or reopens something a
    human already resolved or dismissed."""
    records = db.query(EvidenceRecord).filter(EvidenceRecord.engagement_id == engagement.id).all()
    if not records:
        return AgentStepOutcome(OrchestrationStepStatus.SKIPPED, "No evidence records to assess yet.")

    evidence_like = [
        fraud_risk_service.EvidenceLike(
            id=r.id, doc_type=r.doc_type, vendor_name=r.vendor_name,
            reference_number=r.reference_number, amount=r.amount, record_date=r.record_date,
        )
        for r in records
    ]
    results = fraud_risk_service.run_fraud_risk_detection(evidence_like)

    existing = db.query(FraudRiskFlag).filter(FraudRiskFlag.engagement_id == engagement.id).all()
    existing_keys = {(f.flag_type.value, f.evidence_record_ids) for f in existing}

    new_count = 0
    for result in results:
        ids_str = ",".join(str(i) for i in sorted(result.evidence_record_ids))
        key = (result.flag_type, ids_str)
        if key in existing_keys:
            continue
        db.add(FraudRiskFlag(
            engagement_id=engagement.id, client_id=engagement.client_id,
            flag_type=result.flag_type, description=result.description,
            evidence_record_ids=ids_str, severity=result.severity,
        ))
        new_count += 1
    db.commit()

    detail = f"{len(results)} risk signals found, {new_count} new"
    audit_log_service.log(
        db, actor="fraud_risk_agent", action="fraud_risk_run", detail=detail,
        engagement_id=engagement.id, client_id=engagement.client_id,
    )
    return AgentStepOutcome(OrchestrationStepStatus.SUCCESS, detail)


def run_controls_testing_agent(db: Session, engagement: Engagement) -> AgentStepOutcome:
    active_controls = db.query(Control).filter(Control.engagement_id == engagement.id, Control.active == "active").all()
    if not active_controls:
        return AgentStepOutcome(OrchestrationStepStatus.SKIPPED, "No active controls defined for this engagement yet.")

    records = db.query(EvidenceRecord).filter(EvidenceRecord.engagement_id == engagement.id).all()
    control_like = [
        controls_svc.ControlLike(id=c.id, rule_type=c.rule_type, threshold_amount=c.threshold_amount)
        for c in active_controls
    ]
    evidence_like = [
        controls_svc.EvidenceLike(
            id=r.id, doc_type=r.doc_type, reference_number=r.reference_number,
            related_reference_number=r.related_reference_number, amount=r.amount, approver_name=r.approver_name,
        )
        for r in records
    ]
    results = controls_svc.run_controls_testing(control_like, evidence_like)

    existing = db.query(ControlTestResult).filter(ControlTestResult.engagement_id == engagement.id).all()
    existing_keys = {(e.control_id, e.evidence_record_id) for e in existing}

    new_pass, new_fail = 0, 0
    for result in results:
        key = (result.control_id, result.evidence_record_id)
        if key in existing_keys:
            continue
        is_pass = result.result == "pass"
        db.add(ControlTestResult(
            control_id=result.control_id, engagement_id=engagement.id, client_id=engagement.client_id,
            evidence_record_id=result.evidence_record_id, result=ControlTestStatus(result.result), detail=result.detail,
            status=ExceptionStatus.RESOLVED if is_pass else ExceptionStatus.OPEN,
            resolved_by="controls_testing_agent" if is_pass else None,
            resolution_note="Passed automatically - evidence satisfies the control." if is_pass else None,
        ))
        new_pass += 1 if is_pass else 0
        new_fail += 0 if is_pass else 1
    db.commit()

    detail = f"{len(results)} results ({new_pass} new pass, {new_fail} new fail)"
    audit_log_service.log(
        db, actor="controls_testing_agent", action="controls_testing_run", detail=detail,
        engagement_id=engagement.id, client_id=engagement.client_id,
    )
    return AgentStepOutcome(OrchestrationStepStatus.SUCCESS, detail)


# ------------------------------------------------------------- orchestrator

def _execute_step(db: Session, run: OrchestrationRun, step_order: int, agent_name: str, fn: Callable, *args) -> AgentStepOutcome:
    started = _now()
    try:
        outcome = fn(*args)
    except Exception as e:  # belt-and-suspenders - each agent already handles its own known failure modes
        logger.exception("Orchestration step %s failed unexpectedly", agent_name)
        outcome = AgentStepOutcome(OrchestrationStepStatus.FAILED, f"Unexpected error: {e}")
    completed = _now()
    db.add(OrchestrationStep(
        run_id=run.id, engagement_id=run.engagement_id, client_id=run.client_id,
        step_order=step_order, agent_name=agent_name, status=outcome.status, detail=outcome.detail,
        started_at=started, completed_at=completed,
    ))
    db.commit()
    return outcome


def _skip_step(db: Session, run: OrchestrationRun, step_order: int, agent_name: str, reason: str) -> None:
    now = _now()
    db.add(OrchestrationStep(
        run_id=run.id, engagement_id=run.engagement_id, client_id=run.client_id,
        step_order=step_order, agent_name=agent_name, status=OrchestrationStepStatus.SKIPPED,
        detail=reason, started_at=now, completed_at=now,
    ))
    db.commit()


def run_document_pipeline(db: Session, document: Document, engagement: Engagement) -> OrchestrationRun:
    """Triggered by an upload. Extraction always runs first; reconciliation,
    fraud-risk detection, and controls testing only run if extraction
    actually produced evidence - otherwise they're recorded as SKIPPED,
    not silently absent from the run's history."""
    run = OrchestrationRun(
        engagement_id=engagement.id, client_id=engagement.client_id,
        trigger=OrchestrationTrigger.DOCUMENT_UPLOAD, triggered_by=document.filename,
        status=OrchestrationRunStatus.COMPLETED, started_at=_now(),
    )
    db.add(run)
    db.commit()

    extraction_outcome = _execute_step(db, run, 1, "evidence_extraction_agent", run_evidence_extraction_agent, db, document, engagement)

    if extraction_outcome.status == OrchestrationStepStatus.SUCCESS:
        recon_outcome = _execute_step(db, run, 2, "reconciliation_agent", run_reconciliation_agent, db, engagement)
        fraud_outcome = _execute_step(db, run, 3, "fraud_risk_agent", run_fraud_risk_agent, db, engagement)
        controls_outcome = _execute_step(db, run, 4, "controls_testing_agent", run_controls_testing_agent, db, engagement)
        failed = OrchestrationStepStatus.FAILED in (recon_outcome.status, fraud_outcome.status, controls_outcome.status)
    else:
        _skip_step(db, run, 2, "reconciliation_agent", "Skipped - evidence extraction did not succeed.")
        _skip_step(db, run, 3, "fraud_risk_agent", "Skipped - evidence extraction did not succeed.")
        _skip_step(db, run, 4, "controls_testing_agent", "Skipped - evidence extraction did not succeed.")
        failed = extraction_outcome.status == OrchestrationStepStatus.FAILED

    run.status = OrchestrationRunStatus.FAILED if failed else OrchestrationRunStatus.COMPLETED
    run.completed_at = _now()
    db.commit()

    audit_log_service.log(
        db, actor="orchestrator", action="orchestration_run_completed",
        detail=f"trigger=document_upload document={document.filename} status={run.status.value}",
        engagement_id=engagement.id, client_id=engagement.client_id,
    )
    return run


def run_full_engagement_check(db: Session, engagement: Engagement, triggered_by: str) -> OrchestrationRun:
    """A human-triggered re-run of reconciliation + fraud-risk detection +
    controls testing across ALL existing evidence - useful after defining
    a new control or changing one, without needing to re-upload anything."""
    run = OrchestrationRun(
        engagement_id=engagement.id, client_id=engagement.client_id,
        trigger=OrchestrationTrigger.MANUAL, triggered_by=triggered_by,
        status=OrchestrationRunStatus.COMPLETED, started_at=_now(),
    )
    db.add(run)
    db.commit()

    recon_outcome = _execute_step(db, run, 1, "reconciliation_agent", run_reconciliation_agent, db, engagement)
    fraud_outcome = _execute_step(db, run, 2, "fraud_risk_agent", run_fraud_risk_agent, db, engagement)
    controls_outcome = _execute_step(db, run, 3, "controls_testing_agent", run_controls_testing_agent, db, engagement)
    failed = OrchestrationStepStatus.FAILED in (recon_outcome.status, fraud_outcome.status, controls_outcome.status)

    run.status = OrchestrationRunStatus.FAILED if failed else OrchestrationRunStatus.COMPLETED
    run.completed_at = _now()
    db.commit()

    audit_log_service.log(
        db, actor=triggered_by, action="orchestration_run_completed",
        detail=f"trigger=manual status={run.status.value}",
        engagement_id=engagement.id, client_id=engagement.client_id,
    )
    return run
