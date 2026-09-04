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
    Boolean, Column, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """A real auditor account - new scope beyond the original 5-phase
    roadmap, added so every 'resolved by' / 'finalized by' / 'triggered
    by' field in this app is a real, authenticated person instead of a
    typed name anyone could type incorrectly (or as someone else).
    Deactivated (is_active=False), never deleted - every table above
    that stores a person's name as plain text (resolved_by, etc.) does
    so by name, not by a foreign key, specifically so those historical
    records stay readable even if the account behind them is later
    deactivated."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    email = Column(String(300), nullable=False, unique=True, index=True)
    password_hash = Column(String(200), nullable=False)
    password_salt = Column(String(64), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_now)


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
    approver_name = Column(String(200), nullable=True)  # who signed off, if the document shows one - feeds controls_testing_service
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


class ControlRuleType(str, enum.Enum):
    """Phase 2. Each value corresponds to one deterministic check
    controls_testing_service.py knows how to run - adding a new kind
    of control means adding a new value here AND a new branch in that
    service, never just typing a new string into a form."""
    PO_REQUIRED_ABOVE_THRESHOLD = "po_required_above_threshold"
    APPROVAL_REQUIRED_ABOVE_THRESHOLD = "approval_required_above_threshold"


class Control(Base):
    """One internal control the auditor has decided to test this
    engagement against - e.g. "invoices over $1,000 require a PO" or
    "payments over $5,000 require documented approval". Defined by a
    human (a real control-testing agent would still have a human
    approve the control library, per the blueprint's own permission
    matrix), tested by plain code, never by an LLM."""
    __tablename__ = "controls"

    id = Column(Integer, primary_key=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    rule_type = Column(Enum(ControlRuleType), nullable=False)
    threshold_amount = Column(Float, nullable=False, default=0.0)
    active = Column(String(10), default="active")  # "active" / "inactive" - kept a plain string, no need for a real enum yet
    created_at = Column(DateTime, default=_now)

    test_results = relationship("ControlTestResult", back_populates="control", cascade="all, delete-orphan")


class ControlTestStatus(str, enum.Enum):
    PASS = "pass"
    FAIL = "fail"


class ControlTestResult(Base):
    """One control checked against one evidence record. A PASS is
    logged and auto-closed immediately (nothing for a human to review -
    the evidence already satisfies the control); a FAIL starts OPEN and
    sits in the same kind of review queue as a ReconciliationException,
    closed only by a named human, same discipline throughout this app."""
    __tablename__ = "control_test_results"

    id = Column(Integer, primary_key=True)
    control_id = Column(Integer, ForeignKey("controls.id"), nullable=False, index=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    evidence_record_id = Column(Integer, ForeignKey("evidence_records.id"), nullable=False, index=True)
    result = Column(Enum(ControlTestStatus), nullable=False)
    detail = Column(Text, nullable=False)
    status = Column(Enum(ExceptionStatus), default=ExceptionStatus.OPEN)  # OPEN only meaningful for a FAIL; a PASS is written RESOLVED immediately
    resolved_by = Column(String(200), nullable=True)
    resolution_note = Column(Text, nullable=True)
    tested_at = Column(DateTime, default=_now)
    resolved_at = Column(DateTime, nullable=True)

    control = relationship("Control", back_populates="test_results")


class WorkpaperStatus(str, enum.Enum):
    DRAFT = "draft"
    FINALIZED = "finalized"


class Workpaper(Base):
    """Phase 3. One draft summary memo per engagement - the write-up an
    auditor would otherwise type from scratch by re-reading every
    document, exception, and control result. workpaper_service.py
    builds the actual facts deterministically (counts, statuses,
    resolution notes already on file); the LLM only turns that into
    readable prose. A human can freely edit the draft before finalizing
    it, and finalizing - the one irreversible step - is a named human
    action, logged like every other consequential action in this app."""
    __tablename__ = "workpapers"

    id = Column(Integer, primary_key=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False, unique=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    content = Column(Text, nullable=True)
    status = Column(Enum(WorkpaperStatus), default=WorkpaperStatus.DRAFT)
    generated_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
    finalized_by = Column(String(200), nullable=True)
    finalized_at = Column(DateTime, nullable=True)


class PBCStatus(str, enum.Enum):
    """PBC = "Provided By Client" - the auditor's own term for a request
    sent to the client (a document, a schedule, an explanation) that
    the client, not the auditor, has to fulfill."""
    REQUESTED = "requested"
    RECEIVED = "received"
    WAIVED = "waived"  # the auditor decided this item is no longer needed


class PBCRequest(Base):
    """Phase 4. One item on the request list sent to a client for this
    engagement. Deliberately more standalone than Phases 1-3: it
    doesn't require any evidence extraction or reconciliation to exist,
    though a received item can optionally link to a Document that was
    actually uploaded to satisfy it. "Overdue" is never stored - it's
    computed on the fly from due_date vs. today (see pbc_service.py) so
    it's always accurate and never needs a background job to update it.
    Only a human marks an item received or waived; nothing here is ever
    set by an agent."""
    __tablename__ = "pbc_requests"

    id = Column(Integer, primary_key=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    item_name = Column(String(300), nullable=False)
    description = Column(Text, nullable=True)
    due_date = Column(Date, nullable=True)
    status = Column(Enum(PBCStatus), default=PBCStatus.REQUESTED)
    linked_document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    resolved_by = Column(String(200), nullable=True)
    resolution_note = Column(Text, nullable=True)
    requested_at = Column(DateTime, default=_now)
    resolved_at = Column(DateTime, nullable=True)

    linked_document = relationship("Document")


class FraudFlagType(str, enum.Enum):
    """Each value maps to one deterministic heuristic in
    fraud_risk_service.py - plain pattern-matching over evidence already
    on file, never an LLM call. The blueprint is explicit that this
    agent "should not automatically declare fraud," and this project's
    own history backs that up: a reasoning-model-based fraud feature in
    the sibling InvoiceIQ project was tried and reverted twice for
    being too slow and too unreliable. A flag here is a prompt for a
    human to look closer - never a conclusion."""
    DUPLICATE_PAYMENT_RISK = "duplicate_payment_risk"    # same vendor + same amount, different reference numbers
    ROUND_DOLLAR_AMOUNT = "round_dollar_amount"          # a suspiciously exact, round total
    WEEKEND_TRANSACTION = "weekend_transaction"          # dated a Saturday or Sunday
    NEW_VENDOR_LARGE_AMOUNT = "new_vendor_large_amount"  # a vendor's only appearance, above a materiality threshold


class FraudRiskFlag(Base):
    """One risk signal the deterministic fraud_risk_service.py engine
    found - not a finding, not an accusation, just a pattern worth a
    human's attention. Reuses ExceptionStatus (open/resolved/dismissed)
    for the exact same review-queue discipline as every other flag in
    this app: only a named human closes one, never the engine itself."""
    __tablename__ = "fraud_risk_flags"

    id = Column(Integer, primary_key=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    flag_type = Column(Enum(FraudFlagType), nullable=False)
    description = Column(Text, nullable=False)
    evidence_record_ids = Column(String(200), nullable=True)  # comma-separated ids, same convention as ReconciliationException
    severity = Column(String(20), default="medium")  # low, medium, high
    status = Column(Enum(ExceptionStatus), default=ExceptionStatus.OPEN)
    resolved_by = Column(String(200), nullable=True)
    resolution_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)
    resolved_at = Column(DateTime, nullable=True)


class OrchestrationTrigger(str, enum.Enum):
    DOCUMENT_UPLOAD = "document_upload"
    MANUAL = "manual"


class OrchestrationRunStatus(str, enum.Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class OrchestrationStepStatus(str, enum.Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


class OrchestrationRun(Base):
    """Phase 5. Every agent this run coordinates already existed and
    already made its own deterministic or narrow-LLM decisions in
    Phases 1-2 - an OrchestrationRun adds no new judgment of its own.
    What it adds is the thing a diagram of "Human Auditor -> Orchestrator
    -> testing agents -> Human Review" can't give you by itself: a
    persisted, inspectable record that the right agents actually ran,
    in the right order, with a real result, every single time - not
    just an implicit sequence of function calls buried in a route
    handler. This app has no background job queue, so a run is always
    synchronous start-to-finish within one request; there is no
    "in progress" status to represent."""
    __tablename__ = "orchestration_runs"

    id = Column(Integer, primary_key=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    trigger = Column(Enum(OrchestrationTrigger), nullable=False)
    triggered_by = Column(String(300), nullable=True)  # a filename (upload) or a human's name (manual)
    status = Column(Enum(OrchestrationRunStatus), nullable=False)
    started_at = Column(DateTime, default=_now)
    completed_at = Column(DateTime, nullable=True)

    steps = relationship("OrchestrationStep", back_populates="run", cascade="all, delete-orphan", order_by="OrchestrationStep.step_order")


class OrchestrationStep(Base):
    """One agent's turn within one OrchestrationRun. A step is SKIPPED,
    not silently absent, when an earlier step in the same run failed or
    found nothing for it to do - the full intended pipeline is always
    visible in the record, never just the steps that happened to run."""
    __tablename__ = "orchestration_steps"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("orchestration_runs.id"), nullable=False, index=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    step_order = Column(Integer, nullable=False)
    agent_name = Column(String(100), nullable=False)  # e.g. "evidence_extraction_agent", "reconciliation_agent"
    status = Column(Enum(OrchestrationStepStatus), nullable=False)
    detail = Column(Text, nullable=True)
    started_at = Column(DateTime, default=_now)
    completed_at = Column(DateTime, nullable=True)

    run = relationship("OrchestrationRun", back_populates="steps")


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
