"""
Document Intake Agent, per the blueprint's own spec: "Classifies
incoming audit documents, identifies file types, extracts metadata,
and routes each document to the correct client, engagement, audit
area, and evidence folder." Client/engagement routing already happens
at upload time (every Document row is created under one engagement's
URL - see routes.py) - what this file adds is the file-type check and
the audit-area/evidence-folder tag the blueprint calls out by name.

100% deterministic, zero LLM - classification here means "which folder
does this drop into," a fixed lookup, never "what does this document
actually say" (that's evidence_extraction_service.py's job entirely,
and stays a separate step run afterward). Matches the permission
matrix's own line for this agent: "Read, classify, tag, route
documents. No deleting evidence; no final audit conclusions."

Runs twice per document, deliberately: once at intake (file type only -
nothing else is known yet, so audit_area starts UNCATEGORIZED), and
again right after evidence extraction determines doc_type (see
orchestration_service.py), which is when a real audit-area tag becomes
possible. Same "sort unopened mail by envelope, re-file once you've
actually opened it" two-pass idea a real intake clerk would use.
"""

from app.models.models import AuditArea, DocumentType

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
_PDF_EXTENSION = ".pdf"


def classify_file_kind(filename: str) -> str:
    """Plain extension check - the same test routes.py and
    orchestration_service.py already use to decide the extraction path,
    surfaced here as its own named, loggable intake decision rather
    than staying an unlabeled implementation detail."""
    name = (filename or "").lower()
    if name.endswith(_IMAGE_EXTENSIONS):
        return "image"
    if name.endswith(_PDF_EXTENSION):
        return "pdf"
    return "unknown"


# Deliberately thin - see AuditArea's own docstring for why. Every
# doc_type this app actually extracts today maps to exactly one of two
# real folders; anything not yet known (or a type extraction couldn't
# pin down) lands in UNCATEGORIZED rather than a guess.
_DOC_TYPE_TO_AREA = {
    DocumentType.PURCHASE_ORDER: AuditArea.VENDOR_PAYMENTS,
    DocumentType.INVOICE: AuditArea.VENDOR_PAYMENTS,
    DocumentType.PAYMENT: AuditArea.VENDOR_PAYMENTS,
    DocumentType.BANK_STATEMENT: AuditArea.BANKING_CASH,
}


def route_to_audit_area(doc_type: DocumentType | None) -> AuditArea:
    if doc_type is None:
        return AuditArea.UNCATEGORIZED
    return _DOC_TYPE_TO_AREA.get(doc_type, AuditArea.UNCATEGORIZED)
