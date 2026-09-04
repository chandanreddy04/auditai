"""
Pulls the text layer out of an uploaded PDF, deterministically - no LLM
involved. If a document has no usable text layer (a scanned page with
no OCR text baked in), has_extractable_text() says so up front, and
render_pdf_page_to_image() renders that page to a PNG instead so a
vision-capable model can read it directly (see
evidence_extraction_service.extract_evidence_from_image()) - the same
two-step gate + fallback already proven out in InvoiceIQ.
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


def render_pdf_page_to_image(file_path: str, page_number: int = 0) -> bytes:
    """The fallback path when has_extractable_text() says no: render the
    page as a PNG so a vision-capable model can read it directly instead
    of the pipeline just giving up. 150 DPI is enough resolution for a
    model to read normal printed document text without producing an
    unnecessarily large image - same setting already proven out in
    InvoiceIQ."""
    doc = fitz.open(file_path)
    try:
        pixmap = doc[page_number].get_pixmap(dpi=150)
        return pixmap.tobytes("png")
    finally:
        doc.close()
