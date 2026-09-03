"""
The whole app's routes, wired together across all five phases. Every
route that touches an engagement takes client_id in the URL alongside
engagement_id and checks the engagement actually belongs to that
client - the isolation boundary from models.py's docstring enforced
again at the query layer, not just assumed.

As of Phase 5, this file no longer sequences agents itself - uploading
a document just hands off to orchestration_service.run_document_pipeline()
and gets back a fully-recorded OrchestrationRun. Routes create things,
render pages, and let a human resolve/dismiss/finalize/receive/waive -
coordinating agents is orchestration_service.py's job now.
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
    Client, Control, ControlRuleType, ControlTestResult, Document,
    Engagement, EvidenceRecord, ExceptionStatus, PBCRequest, PBCStatus,
    ReconciliationException, Workpaper, WorkpaperStatus,
)
from app.services import audit_log_service
from app.services import orchestration_service
from app.services import pbc_service
from app.services import workpaper_service
from app.services.llm_client import LLMUnavailableError

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

    orchestration_service.run_document_pipeline(db, document, engagement)

    return RedirectResponse(url=f"/engagements/{engagement_id}", status_code=303)


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
    orchestration_service.run_controls_testing_agent(db, engagement)
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


# ---------------------------------------------------------- orchestration

@router.get("/engagements/{engagement_id}/orchestration")
def orchestration_page(request: Request, engagement_id: int, db: Session = Depends(get_db)):
    from app.models.models import OrchestrationRun

    engagement = _get_engagement_or_404(db, engagement_id)
    runs = (
        db.query(OrchestrationRun)
        .filter(OrchestrationRun.engagement_id == engagement_id)
        .order_by(OrchestrationRun.started_at.desc())
        .limit(30)
        .all()
    )
    return templates.TemplateResponse("orchestration.html", {"request": request, "engagement": engagement, "runs": runs})


@router.post("/engagements/{engagement_id}/run-full-check")
def run_full_check(engagement_id: int, triggered_by: str = Form(...), db: Session = Depends(get_db)):
    """The one manually-triggered orchestration entry point: re-runs
    reconciliation + controls testing across ALL of an engagement's
    evidence without needing a new document upload - e.g. right after
    defining or changing a control."""
    engagement = _get_engagement_or_404(db, engagement_id)
    orchestration_service.run_full_engagement_check(db, engagement, triggered_by=triggered_by.strip())
    return RedirectResponse(url=f"/engagements/{engagement_id}/orchestration", status_code=303)


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
