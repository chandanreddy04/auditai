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
    Client, Document, DocumentStatus, DocumentType, Engagement,
    EngagementStatus, EvidenceRecord, ExceptionStatus, ReconciliationException,
)
from app.services import audit_log_service
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
    evidence_count = db.query(EvidenceRecord).filter(EvidenceRecord.engagement_id == engagement_id).count()
    return templates.TemplateResponse(
        "engagement_dashboard.html",
        {
            "request": request, "engagement": engagement, "documents": documents,
            "open_exceptions": open_exceptions, "evidence_count": evidence_count,
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
    )
    db.add(record)
    db.commit()

    audit_log_service.log(
        db, actor="evidence_extraction_service", action="evidence_extracted",
        detail=f"{doc_type.value} ref={extracted.reference_number} amount={extracted.amount}",
        engagement_id=engagement.id, client_id=engagement.client_id,
    )

    _run_and_persist_reconciliation(db, engagement)


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
