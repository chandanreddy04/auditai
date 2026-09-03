"""
Pulls the text layer out of an uploaded PDF, deterministically - no LLM
involved. If a document has no usable text layer (a scanned page with
no OCR text baked in), has_extractable_text() says so up front and the
route marks the document as failed/unreadable rather than silently
sending an empty string to the LLM and getting a hallucinated result
back. Vision-based extraction for scanned documents is the known
Phase 1 gap noted in the README - the same technique InvoiceIQ already
proved out is a straightforward fast-follow, deliberately left out of
this first slice to keep it narrow.
"""

import fitz  # PyMuPDF

MIN_TEXT_LAYER_CHARS = 20


def extract_text(file_path: str) -> str:
    doc = fitz.open(file_path)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def has_extractable_text(text: str) -> bool:
    return len(text.strip()) >= MIN_TEXT_LAYER_CHARS
