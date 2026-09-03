"""
Text in, structured LLMExtractedEvidence out - one job, nothing else.
No memory, no tools, no decision-making: it does not decide whether a
document reconciles with anything, it only reads what's on the page.
That is deliberate - see Section 1/36-style reasoning already applied
throughout this project's sibling app: extraction is a service, not an
agent, because it has no goal of its own and nothing to decide under
uncertainty. reconciliation_service.py is where real decisions happen,
in plain deterministic Python.
"""

import json
import logging
import re

from app.schemas.extraction import LLMExtractedEvidence
from app.services.llm_client import LLMUnavailableError, chat

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You extract structured data from one page of audit evidence - a "
    "purchase order, invoice, payment record, or bank statement. Read "
    "the text and fill in the fields as accurately as possible. If a "
    "field is not present in the text, leave it null. Dates must be in "
    "YYYY-MM-DD format. Never invent a reference number, amount, or "
    "vendor name that does not actually appear in the text.\n\n"
    "doc_type MUST be exactly one of these five lowercase, underscored "
    "strings - never a human-readable label: purchase_order, invoice, "
    "payment, bank_statement, unknown. For example a document titled "
    "'PURCHASE ORDER' gets doc_type='purchase_order', NOT 'Purchase "
    "Order'. Use 'unknown' only if you truly cannot tell.\n\n"
    "vendor_name is the company name the document is FROM or addressed "
    "TO as the supplier/payee - look for a line labeled 'Vendor:', "
    "'Supplier:', 'Payee:', or 'From:'. It is almost always present; "
    "only leave it null if genuinely no company name appears anywhere.\n\n"
    "reference_number is THIS document's own number (its PO #, invoice "
    "#, or payment/check/transaction #). related_reference_number is a "
    "DIFFERENT document's number that this one points back to - for "
    "example an invoice that prints 'PO #4521' has reference_number = "
    "the invoice's own number and related_reference_number = '4521'. "
    "Do not confuse the two.\n\n"
    "currency is a 3-letter ISO code (USD, EUR, GBP, ...). If no "
    "currency is stated anywhere but a dollar amount ($) appears, use "
    "'USD' - never write the word 'unknown' into this field.\n\n"
    "approver_name is the name of whoever approved/authorized/signed "
    "this document - look for 'Approved by:', 'Authorized by:', "
    "'Signed:', or a signature block with a name under it. Leave it "
    "null if the document shows no approval or signature at all."
)

# Small models are reliable at reading the right words off the page but
# not always at matching an exact output vocabulary, even with a strict
# prompt and a constrained schema - the doc_type/currency slips seen in
# real testing (e.g. 'Purchase Order' instead of 'purchase_order',
# 'unknown' written into currency) are the model's formatting, not its
# reading. This is exactly the same "don't trust the model's formatting,
# only its extraction" lesson already applied throughout this stack -
# normalize deterministically in code rather than fight it with an ever
# longer prompt.
_DOC_TYPE_ALIASES = {
    "purchase order": "purchase_order", "po": "purchase_order",
    "invoice": "invoice", "bill": "invoice",
    "payment": "payment", "payment receipt": "payment", "remittance advice": "payment", "receipt": "payment",
    "bank statement": "bank_statement", "statement": "bank_statement",
}
_VALID_DOC_TYPES = {"purchase_order", "invoice", "payment", "bank_statement", "unknown"}
_VALID_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")


def _normalize_doc_type(raw: str) -> str:
    key = (raw or "").strip().lower()
    if key in _VALID_DOC_TYPES:
        return key
    key_underscored = key.replace(" ", "_").replace("-", "_")
    if key_underscored in _VALID_DOC_TYPES:
        return key_underscored
    if key in _DOC_TYPE_ALIASES:
        return _DOC_TYPE_ALIASES[key]
    return "unknown"


def _normalize_currency(raw: str) -> str:
    if raw and _VALID_CURRENCY_RE.match(raw.strip()):
        return raw.strip().upper()
    return "USD"


def extract_evidence(raw_text: str) -> LLMExtractedEvidence:
    try:
        content = chat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Document text:\n\n{raw_text}"},
            ],
            schema=LLMExtractedEvidence.model_json_schema(),
        )
    except LLMUnavailableError as e:
        logger.warning("Evidence extraction failed: %s", e)
        raise

    try:
        data = json.loads(content)
        result = LLMExtractedEvidence.model_validate(data)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("LLM returned invalid structured output: %s", e)
        raise LLMUnavailableError(f"Model output did not match schema: {e}") from e

    result.doc_type = _normalize_doc_type(result.doc_type)
    result.currency = _normalize_currency(result.currency)
    return result
