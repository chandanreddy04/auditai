"""
Phase 1 data model. Every table below client-level carries BOTH
client_id and engagement_id directly (not just reachable through a
join) - deliberately denormalized. The blueprint's own requirement
(section 9, "working safely across 4-5 clients") is that client
isolation can't be something a query might forget to apply; every
evidence row, exception, and log line has to prove which client it
belongs to just by existing. A query that filters by client_id is
always one WHERE clause away, never a multi-table join away.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Client(Base):
    """One of the 4-5 companies being audited. The root of the isolation
    boundary - every other table hangs off this, directly or via
    engagement_id."""
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    created_at = Column(DateTime, default=_now)

    engagements = relationship("Engagement", back_populates="client", cascade="all, delete-orphan")


class EngagementStatus(str, enum.Enum):
    PLANNING = "planning"
    FIELDWORK = "fieldwork"
    REVIEW = "review"
    CLOSED = "closed"


class Engagement(Base):
    """One audit project for one client (e.g. "Acme Corp - FY2026
    Financial Audit"). Documents, evidence, and exceptions all belong
    to exactly one engagement - this is the unit auditors actually
    work inside day to day."""
    __tablename__ = "engagements"

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    audit_type = Column(String(50), default="financial")  # financial, internal_controls, compliance, ...
    status = Column(Enum(EngagementStatus), default=EngagementStatus.FIELDWORK)
    created_at = Column(DateTime, default=_now)

    client = relationship("Client", back_populates="engagements")
    documents = relationship("Document", back_populates="engagement", cascade="all, delete-orphan")


class DocumentType(str, enum.Enum):
    PURCHASE_ORDER = "purchase_order"
    INVOICE = "invoice"
    PAYMENT = "payment"
    BANK_STATEMENT = "bank_statement"
    UNKNOWN = "unknown"


class DocumentStatus(str, enum.Enum):
    UPLOADED = "uploaded"
    EXTRACTED = "extracted"
    FAILED = "failed"


class Document(Base):
    """One uploaded piece of evidence (a PDF, for Phase 1). client_id is
    copied down from the engagement at write time - denormalized on
    purpose, see the module docstring."""
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    filename = Column(String(300), nullable=False)
    file_path = Column(String(500), nullable=False)
    doc_type = Column(Enum(DocumentType), default=DocumentType.UNKNOWN)
    status = Column(Enum(DocumentStatus), default=DocumentStatus.UPLOADED)
    raw_text = Column(Text, nullable=True)
    failure_reason = Column(String(500), nullable=True)
    uploaded_at = Column(DateTime, default=_now)

    engagement = relationship("Engagement", back_populates="documents")
    evidence_records = relationship("EvidenceRecord", back_populates="document", cascade="all, delete-orphan")


class EvidenceRecord(Base):
    """The structured fields the Evidence Extraction step pulled out of
    one document - this is what the Reconciliation engine actually
    compares. One document normally produces one evidence record in
    Phase 1 (a document IS one invoice/PO/payment); multi-line-item
    documents can still produce one record per line in a later phase."""
    __tablename__ = "evidence_records"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    doc_type = Column(Enum(DocumentType), nullable=False)
    vendor_name = Column(String(200), nullable=True)
    reference_number = Column(String(100), nullable=True)  # PO#, invoice#, payment/check#
    related_reference_number = Column(String(100), nullable=True)  # e.g. an invoice's own "PO#: ..." field
    amount = Column(Float, nullable=True)
    currency = Column(String(10), default="USD")
    record_date = Column(String(20), nullable=True)  # ISO date string, as extracted
    extracted_at = Column(DateTime, default=_now)

    document = relationship("Document", back_populates="evidence_records")


class ExceptionType(str, enum.Enum):
    MISSING_MATCH = "missing_match"        # invoice with no matching PO, payment with no matching invoice, etc.
    AMOUNT_MISMATCH = "amount_mismatch"     # matched pair, amounts differ beyond tolerance
    DUPLICATE = "duplicate"                 # same reference number + doc_type appears more than once
    UNREADABLE = "unreadable"               # extraction failed / low-confidence


class ExceptionStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ReconciliationException(Base):
    """One thing the deterministic reconciliation engine could not
    reconcile cleanly - the entire point of Phase 1. Never created or
    closed by an LLM; the engine in reconciliation_service.py decides
    what counts as an exception using plain comparisons, and only a
    human can resolve or dismiss one (resolved_by is a person, never
    an agent name)."""
    __tablename__ = "reconciliation_exceptions"

    id = Column(Integer, primary_key=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    exception_type = Column(Enum(ExceptionType), nullable=False)
    description = Column(Text, nullable=False)
    evidence_record_ids = Column(String(200), nullable=True)  # comma-separated ids, kept simple for Phase 1
    severity = Column(String(20), default="medium")  # low, medium, high
    status = Column(Enum(ExceptionStatus), default=ExceptionStatus.OPEN)
    resolved_by = Column(String(200), nullable=True)
    resolution_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)
    resolved_at = Column(DateTime, nullable=True)


class AuditLogEntry(Base):
    """Append-only. Every agent action and every human decision writes
    one row here - who/what did it, on what evidence, when. Nothing
    ever updates or deletes a row in this table; that's what makes it
    an audit trail rather than just a log."""
    __tablename__ = "audit_log_entries"

    id = Column(Integer, primary_key=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True, index=True)
    actor = Column(String(100), nullable=False)  # e.g. "extraction_agent", "reconciliation_engine", or a person's name
    action = Column(String(100), nullable=False)  # e.g. "document_uploaded", "evidence_extracted", "exception_resolved"
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)
