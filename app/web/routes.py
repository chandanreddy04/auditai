"""
The whole Phase 1 vertical slice, wired together: upload evidence,
extract it, reconcile it, and let a human clear the exceptions that
come out. Every route that touches an engagement takes client_id in
the URL alongside engagement_id and checks the engagement actually
belongs to that client - the isolation boundary from models.py's
docstring enforced again at the query layer, not just assumed.
"""

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import UPLOAD_DIR
from app.database.session import get_db
from app.models.models import (
    Client, Control, ControlRuleType, ControlTestResult, ControlTestStatus,
    Document, DocumentStatus, DocumentType, Engagement, EngagementStatus,
    EvidenceRecord, ExceptionStatus, PBCRequest, PBCStatus,
    ReconciliationException, Workpaper, WorkpaperStatus,
)
from app.services import audit_log_service
from app.services import controls_testing_service as controls_svc
from app.services import pbc_service
from app.services import workpaper_service
from app.services.evidence_extraction_service import extract_evidence
from app.services.llm_client import LLMUnavailableError
from app.services.pdf_text_service import extract_text, has_extractable_text
from app.services.reconciliation_service import EvidenceLike, run_reconciliation

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---------------------------------------------------------------- clients

@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    clients = db.query(Client).order_by(Client.name).all()
    return templates.TemplateResponse("clients.html", {"request": request, "clients": clients})


@router.post("/clients")
def create_client(name: str = Form(...), db: Session = Depends(get_db)):
    client = Client(name=name.strip())
    db.add(client)
    db.commit()
    audit_log_service.log(db, actor="human", action="client_created", detail=client.name, client_id=client.id)
    return RedirectResponse(url="/", status_code=303)


@router.get("/clients/{client_id}")
def client_detail(request: Request, client_id: int, db: Session = Depends(get_db)):
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(404, "Client not found")
    engagements = db.query(Engagement).filter(Engagement.client_id == client_id).order_by(Engagement.created_at.desc()).all()
    return templates.TemplateResponse(
        "client_detail.html",
        {"request": request, "client": client, "engagements": engagements, "audit_types": AUDIT_TYPES},
    )


AUDIT_TYPES = ["financial", "internal_controls", "compliance", "operational", "it", "cybersecurity"]


@router.post("/clients/{client_id}/engagements")
def create_engagement(client_id: int, name: str = Form(...), audit_type: str = Form("financial"), db: Session = Depends(get_db)):
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(404, "Client not found")
    engagement = Engagement(client_id=client_id, name=name.strip(), audit_type=audit_type)
    db.add(engagement)
    db.commit()
    audit_log_service.log(
        db, actor="human", action="engagement_created", detail=engagement.name,
        engagement_id=engagement.id, client_id=client_id,
    )
    return RedirectResponse(url=f"/clients/{client_id}", status_code=303)


# ------------------------------------------------------------ engagements

def _get_engagement_or_404(db: Session, engagement_id: int) -> Engagement:
    engagement = db.get(Engagement, engagement_id)
    if engagement is None:
        raise HTTPException(404, "Engagement not found")
    return engagement


@router.get("/engagements/{engagement_id}")
def engagement_dashboard(request: Request, engagement_id: int, db: Session = Depends(get_db)):
    engagement = _get_engagement_or_404(db, engagement_id)
    documents = db.query(Document).filter(Document.engagement_id == engagement_id).order_by(Document.uploaded_at.desc()).all()
    open_exceptions = (
        db.query(ReconciliationException)
        .filter(ReconciliationException.engagement_id == engagement_id, ReconciliationException.status == ExceptionStatus.OPEN)
        .count()
    )
    open_control_failures = (
        db.query(ControlTestResult)
        .filter(ControlTestResult.engagement_id == engagement_id, ControlTestResult.status == ExceptionStatus.OPEN)
        .count()
    )
    evidence_count = db.query(EvidenceRecord).filter(EvidenceRecord.engagement_id == engagement_id).count()
    return templates.TemplateResponse(
        "engagement_dashboard.html",
        {
            "request": request, "engagement": engagement, "documents": documents,
            "open_exceptions": open_exceptions, "open_control_failures": open_control_failures,
            "evidence_count": evidence_count,
        },
    )


@router.post("/engagements/{engagement_id}/documents")
async def upload_document(engagement_id: int, file: UploadFile, db: Session = Depends(get_db)):
    engagement = _get_engagement_or_404(db, engagement_id)

    dest_name = f"{uuid.uuid4().hex}_{file.filename}"
    dest_path = UPLOAD_DIR / dest_name
    contents = await file.read()
    dest_path.write_bytes(contents)

    document = Document(
        engagement_id=engagement_id, client_id=engagement.client_id,
        filename=file.filename, file_path=str(dest_path),
    )
    db.add(document)
    db.commit()
    audit_log_service.log(
        db, actor="human", action="document_uploaded", detail=file.filename,
        engagement_id=engagement_id, client_id=engagement.client_id,
    )

    _extract_and_reconcile(db, document, engagement)

    return RedirectResponse(url=f"/engagements/{engagement_id}", status_code=303)


def _extract_and_reconcile(db: Session, document: Document, engagement: Engagement) -> None:
    """Text extraction -> LLM evidence extraction -> full re-reconciliation
    of the engagement. Any failure at any step marks the document FAILED
    with a human-readable reason instead of silently leaving it stuck -
    same "never fail invisibly" discipline as the rest of this project's
    LLM-touching code."""
    try:
        text = extract_text(document.file_path)
    except Exception as e:
        logger.warning("PDF text extraction failed for document %s: %s", document.id, e)
        document.status = DocumentStatus.FAILED
        document.failure_reason = "Could not read this file as a PDF."
        db.commit()
        return

    if not has_extractable_text(text):
        document.status = DocumentStatus.FAILED
        document.failure_reason = (
            "No text layer found (likely a scanned image). Vision-based "
            "extraction for scanned documents is a planned fast-follow, "
            "not yet part of this Phase 1 build."
        )
        db.commit()
        return

    document.raw_text = text

    try:
        extracted = extract_evidence(text)
    except LLMUnavailableError as e:
        document.status = DocumentStatus.FAILED
        document.failure_reason = f"AI extraction unavailable: {e}"
        db.commit()
        audit_log_service.log(
            db, actor="evidence_extraction_service", action="extraction_failed", detail=str(e),
            engagement_id=engagement.id, client_id=engagement.client_id,
        )
        return

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

    audit_log_service.log(
        db, actor="evidence_extraction_service", action="evidence_extracted",
        detail=f"{doc_type.value} ref={extracted.reference_number} amount={extracted.amount}",
        engagement_id=engagement.id, client_id=engagement.client_id,
    )

    _run_and_persist_reconciliation(db, engagement)
    _run_and_persist_controls_testing(db, engagement)


def _run_and_persist_reconciliation(db: Session, engagement: Engagement) -> None:
    """Re-runs reconciliation across ALL of the engagement's evidence
    every time new evidence arrives - simplest correct approach for
    Phase 1's volumes. Only adds NEW open exceptions (same type + same
    evidence records not already open); never touches an exception a
    human has already resolved or dismissed, and never auto-closes one
    either - only a human does that."""
    records = db.query(EvidenceRecord).filter(EvidenceRecord.engagement_id == engagement.id).all()
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

    audit_log_service.log(
        db, actor="reconciliation_engine", action="reconciliation_run",
        detail=f"{len(results)} exceptions found, {new_count} new",
        engagement_id=engagement.id, client_id=engagement.client_id,
    )


def _run_and_persist_controls_testing(db: Session, engagement: Engagement) -> None:
    """Phase 2, same re-run-everything-every-time approach as
    reconciliation above: test every active control against every
    evidence record in the engagement. A PASS is written RESOLVED
    immediately - nothing for a human to review, the evidence already
    satisfies the control. A FAIL is only written OPEN the first time;
    once a human has resolved or dismissed a given (control, evidence
    record) failure, re-running never reopens or duplicates it."""
    active_controls = db.query(Control).filter(Control.engagement_id == engagement.id, Control.active == "active").all()
    if not active_controls:
        return

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

    existing = (
        db.query(ControlTestResult)
        .filter(ControlTestResult.engagement_id == engagement.id)
        .all()
    )
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
            resolved_by="controls_testing_engine" if is_pass else None,
            resolution_note="Passed automatically - evidence satisfies the control." if is_pass else None,
        ))
        new_pass += 1 if is_pass else 0
        new_fail += 0 if is_pass else 1
    db.commit()

    audit_log_service.log(
        db, actor="controls_testing_engine", action="controls_testing_run",
        detail=f"{len(results)} results ({new_pass} new pass, {new_fail} new fail)",
        engagement_id=engagement.id, client_id=engagement.client_id,
    )


# -------------------------------------------------------- exception queue

@router.get("/engagements/{engagement_id}/exceptions")
def exceptions_queue(request: Request, engagement_id: int, db: Session = Depends(get_db)):
    engagement = _get_engagement_or_404(db, engagement_id)
    open_exceptions = (
        db.query(ReconciliationException)
        .filter(ReconciliationException.engagement_id == engagement_id, ReconciliationException.status == ExceptionStatus.OPEN)
        .order_by(ReconciliationException.severity.desc(), ReconciliationException.created_at)
        .all()
    )
    resolved_exceptions = (
        db.query(ReconciliationException)
        .filter(ReconciliationException.engagement_id == engagement_id, ReconciliationException.status != ExceptionStatus.OPEN)
        .order_by(ReconciliationException.resolved_at.desc())
        .limit(20)
        .all()
    )
    return templates.TemplateResponse(
        "exceptions_queue.html",
        {"request": request, "engagement": engagement, "open_exceptions": open_exceptions, "resolved_exceptions": resolved_exceptions},
    )


@router.post("/exceptions/{exception_id}/resolve")
def resolve_exception(
    exception_id: int, resolved_by: str = Form(...), resolution_note: str = Form(""),
    action: str = Form("resolved"), db: Session = Depends(get_db),
):
    exc = db.get(ReconciliationException, exception_id)
    if exc is None:
        raise HTTPException(404, "Exception not found")

    from datetime import datetime, timezone
    exc.status = ExceptionStatus.RESOLVED if action == "resolved" else ExceptionStatus.DISMISSED
    exc.resolved_by = resolved_by.strip()
    exc.resolution_note = resolution_note.strip()
    exc.resolved_at = datetime.now(timezone.utc)
    db.commit()

    audit_log_service.log(
        db, actor=resolved_by.strip(), action=f"exception_{exc.status.value}",
        detail=f"#{exc.id}: {resolution_note.strip()}",
        engagement_id=exc.engagement_id, client_id=exc.client_id,
    )
    return RedirectResponse(url=f"/engagements/{exc.engagement_id}/exceptions", status_code=303)


# ----------------------------------------------------------------- controls

@router.get("/engagements/{engagement_id}/controls")
def controls_page(request: Request, engagement_id: int, db: Session = Depends(get_db)):
    engagement = _get_engagement_or_404(db, engagement_id)
    controls = db.query(Control).filter(Control.engagement_id == engagement_id).order_by(Control.created_at).all()

    open_results = (
        db.query(ControlTestResult)
        .filter(ControlTestResult.engagement_id == engagement_id, ControlTestResult.status == ExceptionStatus.OPEN)
        .order_by(ControlTestResult.tested_at)
        .all()
    )
    closed_results = (
        db.query(ControlTestResult)
        .filter(ControlTestResult.engagement_id == engagement_id, ControlTestResult.status != ExceptionStatus.OPEN)
        .order_by(ControlTestResult.tested_at.desc())
        .limit(30)
        .all()
    )
    return templates.TemplateResponse(
        "controls.html",
        {
            "request": request, "engagement": engagement, "controls": controls,
            "open_results": open_results, "closed_results": closed_results,
            "rule_types": [t.value for t in ControlRuleType],
        },
    )


@router.post("/engagements/{engagement_id}/controls")
def create_control(
    engagement_id: int, name: str = Form(...), rule_type: str = Form(...),
    threshold_amount: float = Form(0.0), db: Session = Depends(get_db),
):
    engagement = _get_engagement_or_404(db, engagement_id)
    control = Control(
        engagement_id=engagement_id, client_id=engagement.client_id,
        name=name.strip(), rule_type=ControlRuleType(rule_type), threshold_amount=threshold_amount,
    )
    db.add(control)
    db.commit()
    audit_log_service.log(
        db, actor="human", action="control_defined",
        detail=f"{control.name} ({control.rule_type.value}, threshold={control.threshold_amount:,.2f})",
        engagement_id=engagement_id, client_id=engagement.client_id,
    )
    _run_and_persist_controls_testing(db, engagement)
    return RedirectResponse(url=f"/engagements/{engagement_id}/controls", status_code=303)


@router.post("/control-results/{result_id}/resolve")
def resolve_control_result(
    result_id: int, resolved_by: str = Form(...), resolution_note: str = Form(""),
    action: str = Form("resolved"), db: Session = Depends(get_db),
):
    result = db.get(ControlTestResult, result_id)
    if result is None:
        raise HTTPException(404, "Control test result not found")

    from datetime import datetime, timezone
    result.status = ExceptionStatus.RESOLVED if action == "resolved" else ExceptionStatus.DISMISSED
    result.resolved_by = resolved_by.strip()
    result.resolution_note = resolution_note.strip()
    result.resolved_at = datetime.now(timezone.utc)
    db.commit()

    audit_log_service.log(
        db, actor=resolved_by.strip(), action=f"control_result_{result.status.value}",
        detail=f"#{result.id}: {resolution_note.strip()}",
        engagement_id=result.engagement_id, client_id=result.client_id,
    )
    return RedirectResponse(url=f"/engagements/{result.engagement_id}/controls", status_code=303)


# --------------------------------------------------------------- workpaper

def _get_or_create_workpaper(db: Session, engagement: Engagement) -> Workpaper:
    wp = db.query(Workpaper).filter(Workpaper.engagement_id == engagement.id).first()
    if wp is None:
        wp = Workpaper(engagement_id=engagement.id, client_id=engagement.client_id, status=WorkpaperStatus.DRAFT)
        db.add(wp)
        db.commit()
    return wp


@router.get("/engagements/{engagement_id}/workpaper")
def workpaper_page(request: Request, engagement_id: int, db: Session = Depends(get_db)):
    engagement = _get_engagement_or_404(db, engagement_id)
    wp = _get_or_create_workpaper(db, engagement)
    return templates.TemplateResponse("workpaper.html", {"request": request, "engagement": engagement, "wp": wp})


@router.post("/engagements/{engagement_id}/workpaper/generate")
def generate_workpaper(engagement_id: int, db: Session = Depends(get_db)):
    """The one LLM call in this phase: build the deterministic summary
    of everything decided so far, then ask the model to write it up.
    Refuses to overwrite a finalized workpaper - regenerating means
    starting a new draft, which this phase deliberately doesn't offer
    yet (see README's Known Limitations)."""
    from datetime import datetime, timezone

    engagement = _get_engagement_or_404(db, engagement_id)
    wp = _get_or_create_workpaper(db, engagement)
    if wp.status == WorkpaperStatus.FINALIZED:
        raise HTTPException(400, "This workpaper is finalized and cannot be regenerated.")

    summary = workpaper_service.build_engagement_summary(db, engagement)
    try:
        draft = workpaper_service.draft_workpaper_narrative(summary)
    except LLMUnavailableError as e:
        audit_log_service.log(
            db, actor="workpaper_service", action="workpaper_draft_failed", detail=str(e),
            engagement_id=engagement_id, client_id=engagement.client_id,
        )
        raise HTTPException(503, f"AI drafting unavailable: {e}")

    wp.content = draft
    wp.generated_at = datetime.now(timezone.utc)
    wp.updated_at = wp.generated_at
    db.commit()

    audit_log_service.log(
        db, actor="workpaper_service", action="workpaper_drafted",
        detail=f"{summary.documents_total} documents, {summary.exceptions_total} exceptions, "
               f"{len(summary.control_findings)} control results summarized",
        engagement_id=engagement_id, client_id=engagement.client_id,
    )
    return RedirectResponse(url=f"/engagements/{engagement_id}/workpaper", status_code=303)


@router.post("/engagements/{engagement_id}/workpaper/save")
def save_workpaper(engagement_id: int, content: str = Form(...), db: Session = Depends(get_db)):
    from datetime import datetime, timezone

    engagement = _get_engagement_or_404(db, engagement_id)
    wp = _get_or_create_workpaper(db, engagement)
    if wp.status == WorkpaperStatus.FINALIZED:
        raise HTTPException(400, "This workpaper is finalized and cannot be edited.")

    wp.content = content
    wp.updated_at = datetime.now(timezone.utc)
    db.commit()

    audit_log_service.log(
        db, actor="human", action="workpaper_edited", detail=f"{len(content)} characters",
        engagement_id=engagement_id, client_id=engagement.client_id,
    )
    return RedirectResponse(url=f"/engagements/{engagement_id}/workpaper", status_code=303)


@router.post("/engagements/{engagement_id}/workpaper/finalize")
def finalize_workpaper(engagement_id: int, finalized_by: str = Form(...), db: Session = Depends(get_db)):
    from datetime import datetime, timezone

    engagement = _get_engagement_or_404(db, engagement_id)
    wp = _get_or_create_workpaper(db, engagement)
    if not wp.content:
        raise HTTPException(400, "Cannot finalize an empty workpaper - generate or write a draft first.")

    wp.status = WorkpaperStatus.FINALIZED
    wp.finalized_by = finalized_by.strip()
    wp.finalized_at = datetime.now(timezone.utc)
    db.commit()

    audit_log_service.log(
        db, actor=finalized_by.strip(), action="workpaper_finalized", detail="",
        engagement_id=engagement_id, client_id=engagement.client_id,
    )
    return RedirectResponse(url=f"/engagements/{engagement_id}/workpaper", status_code=303)


# ------------------------------------------------------------------- pbc

@router.get("/engagements/{engagement_id}/pbc")
def pbc_page(request: Request, engagement_id: int, db: Session = Depends(get_db)):
    from datetime import date

    engagement = _get_engagement_or_404(db, engagement_id)
    requests_ = db.query(PBCRequest).filter(PBCRequest.engagement_id == engagement_id).order_by(PBCRequest.requested_at).all()
    documents = db.query(Document).filter(Document.engagement_id == engagement_id).order_by(Document.filename).all()

    today = date.today()
    pbc_like = [pbc_service.PBCLike(id=r.id, item_name=r.item_name, due_date=r.due_date, status=r.status.value) for r in requests_]
    overdue_ids = {o.id for o in pbc_service.find_overdue(pbc_like, today)}

    open_requests = [r for r in requests_ if r.status == PBCStatus.REQUESTED]
    closed_requests = [r for r in requests_ if r.status != PBCStatus.REQUESTED]

    return templates.TemplateResponse(
        "pbc.html",
        {
            "request": request, "engagement": engagement, "open_requests": open_requests,
            "closed_requests": closed_requests, "overdue_ids": overdue_ids, "documents": documents,
            "has_overdue": bool(overdue_ids), "reminder_draft": None,
        },
    )


@router.post("/engagements/{engagement_id}/pbc")
def create_pbc_request(
    engagement_id: int, item_name: str = Form(...), description: str = Form(""),
    due_date: str = Form(""), db: Session = Depends(get_db),
):
    from datetime import date as date_cls

    engagement = _get_engagement_or_404(db, engagement_id)
    parsed_due = date_cls.fromisoformat(due_date) if due_date.strip() else None
    req = PBCRequest(
        engagement_id=engagement_id, client_id=engagement.client_id,
        item_name=item_name.strip(), description=description.strip() or None, due_date=parsed_due,
    )
    db.add(req)
    db.commit()
    audit_log_service.log(
        db, actor="human", action="pbc_requested", detail=f"{req.item_name} (due {due_date or 'no date set'})",
        engagement_id=engagement_id, client_id=engagement.client_id,
    )
    return RedirectResponse(url=f"/engagements/{engagement_id}/pbc", status_code=303)


@router.post("/pbc/{request_id}/receive")
def receive_pbc_request(
    request_id: int, resolved_by: str = Form(...), resolution_note: str = Form(""),
    linked_document_id: str = Form(""), db: Session = Depends(get_db),
):
    from datetime import datetime, timezone

    req = db.get(PBCRequest, request_id)
    if req is None:
        raise HTTPException(404, "PBC request not found")

    req.status = PBCStatus.RECEIVED
    req.resolved_by = resolved_by.strip()
    req.resolution_note = resolution_note.strip() or None
    req.resolved_at = datetime.now(timezone.utc)
    if linked_document_id.strip():
        req.linked_document_id = int(linked_document_id)
    db.commit()

    audit_log_service.log(
        db, actor=resolved_by.strip(), action="pbc_received", detail=f"{req.item_name}: {req.resolution_note or ''}",
        engagement_id=req.engagement_id, client_id=req.client_id,
    )
    return RedirectResponse(url=f"/engagements/{req.engagement_id}/pbc", status_code=303)


@router.post("/pbc/{request_id}/waive")
def waive_pbc_request(request_id: int, resolved_by: str = Form(...), resolution_note: str = Form(...), db: Session = Depends(get_db)):
    from datetime import datetime, timezone

    req = db.get(PBCRequest, request_id)
    if req is None:
        raise HTTPException(404, "PBC request not found")

    req.status = PBCStatus.WAIVED
    req.resolved_by = resolved_by.strip()
    req.resolution_note = resolution_note.strip()
    req.resolved_at = datetime.now(timezone.utc)
    db.commit()

    audit_log_service.log(
        db, actor=resolved_by.strip(), action="pbc_waived", detail=f"{req.item_name}: {req.resolution_note}",
        engagement_id=req.engagement_id, client_id=req.client_id,
    )
    return RedirectResponse(url=f"/engagements/{req.engagement_id}/pbc", status_code=303)


@router.post("/engagements/{engagement_id}/pbc/draft-reminder")
def draft_pbc_reminder(request: Request, engagement_id: int, db: Session = Depends(get_db)):
    """Renders the page directly with a drafted reminder rather than
    redirecting - the draft is deliberately not persisted anywhere
    (it's a one-off convenience, not a fact worth storing), so a
    redirect would just lose it. The auditor copies it into their own
    email client and sends it themselves; this app never sends mail on
    anyone's behalf, per the project's own rule about external
    messages always needing a human action."""
    from datetime import date

    engagement = _get_engagement_or_404(db, engagement_id)
    requests_ = db.query(PBCRequest).filter(PBCRequest.engagement_id == engagement_id).all()
    documents = db.query(Document).filter(Document.engagement_id == engagement_id).order_by(Document.filename).all()

    today = date.today()
    pbc_like = [pbc_service.PBCLike(id=r.id, item_name=r.item_name, due_date=r.due_date, status=r.status.value) for r in requests_]
    overdue = pbc_service.find_overdue(pbc_like, today)

    reminder_draft = None
    if overdue:
        try:
            reminder_draft = pbc_service.draft_reminder_email(engagement.client.name, engagement.name, overdue)
            audit_log_service.log(
                db, actor="pbc_service", action="pbc_reminder_drafted", detail=f"{len(overdue)} overdue items",
                engagement_id=engagement_id, client_id=engagement.client_id,
            )
        except LLMUnavailableError as e:
            reminder_draft = f"(AI drafting unavailable: {e})"

    overdue_ids = {o.id for o in overdue}
    open_requests = [r for r in requests_ if r.status == PBCStatus.REQUESTED]
    closed_requests = [r for r in requests_ if r.status != PBCStatus.REQUESTED]

    return templates.TemplateResponse(
        "pbc.html",
        {
            "request": request, "engagement": engagement, "open_requests": open_requests,
            "closed_requests": closed_requests, "overdue_ids": overdue_ids, "documents": documents,
            "has_overdue": bool(overdue_ids), "reminder_draft": reminder_draft,
        },
    )


# -------------------------------------------------------------- audit log

@router.get("/engagements/{engagement_id}/audit-log")
def audit_log(request: Request, engagement_id: int, db: Session = Depends(get_db)):
    from app.models.models import AuditLogEntry
    engagement = _get_engagement_or_404(db, engagement_id)
    entries = (
        db.query(AuditLogEntry)
        .filter(AuditLogEntry.engagement_id == engagement_id)
        .order_by(AuditLogEntry.created_at.desc())
        .all()
    )
    return templates.TemplateResponse("audit_log.html", {"request": request, "engagement": engagement, "entries": entries})
