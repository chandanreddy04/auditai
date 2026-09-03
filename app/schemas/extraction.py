"""
The shape we force the LLM's output into for evidence extraction -
passed straight to Ollama's `format` parameter so the model's reply is
constrained to this schema, same mechanism InvoiceIQ's extraction used.
The LLM's only job is filling this in from a document's text; it never
decides what counts as a match or an exception - that's
reconciliation_service.py, plain Python, no model involved.

Every field below is REQUIRED in the schema (no `default=`), even
though its type allows null - a real, live-found gap, not a stylistic
choice. With a default set, a field is optional in the generated JSON
Schema, and phi3.5 under Ollama's schema-constrained decoding would
routinely just omit amount/vendor_name/reference_number from its reply
entirely rather than write null - cheaper output, technically still
valid against an optional field. Making every field required forces
the model to actually attempt each one (writing null if it truly finds
nothing) instead of silently skipping it.
"""

from pydantic import BaseModel, Field


class LLMExtractedEvidence(BaseModel):
    doc_type: str = Field(
        description="One of: purchase_order, invoice, payment, bank_statement, unknown. "
                    "Judge this from the document's own title/header and content."
    )
    vendor_name: str | None = Field(description="The vendor/supplier company name on the document")
    reference_number: str | None = Field(
        description="This document's OWN identifying number - e.g. the PO number if this "
                    "is a purchase order, the invoice number if this is an invoice, the "
                    "check/transaction number if this is a payment record.",
    )
    related_reference_number: str | None = Field(
        description="A DIFFERENT document's number that this one refers back to - e.g. an "
                    "invoice usually prints the PO number it was issued against, and a "
                    "payment record usually prints the invoice number it's paying. Null if "
                    "the document doesn't reference another document's number.",
    )
    amount: float | None = Field(description="The total amount on the document")
    currency: str = Field(description="3-letter ISO currency code, e.g. USD")
    record_date: str | None = Field(description="ISO format YYYY-MM-DD")
    approver_name: str | None = Field(
        description="The name of the person who approved/authorized this document, if the "
                    "document shows one - e.g. next to 'Approved by:', 'Authorized by:', or "
                    "a signature line. Null if no approval evidence appears anywhere.",
    )
