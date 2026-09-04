import fitz
import pytest

from app.services import pdf_text_service as svc


def _make_pdf(path, text=""):
    doc = fitz.open()
    page = doc.new_page()
    if text:
        page.insert_text((50, 72), text, fontsize=12)
    doc.save(path)
    doc.close()


def test_has_extractable_text_true_for_real_text():
    assert svc.has_extractable_text("This is a real invoice with plenty of text on it.") is True


def test_has_extractable_text_false_for_empty_string():
    assert svc.has_extractable_text("") is False


def test_has_extractable_text_false_for_short_whitespace():
    assert svc.has_extractable_text("   \n  ") is False


def test_extract_text_reads_real_text_layer(tmp_path):
    pdf_path = tmp_path / "with_text.pdf"
    _make_pdf(str(pdf_path), "INVOICE\nInvoice Number: INV-1\nAmount: 100.00")

    text = svc.extract_text(str(pdf_path))
    assert "INVOICE" in text
    assert "INV-1" in text


def test_render_pdf_page_to_image_returns_valid_png(tmp_path):
    pdf_path = tmp_path / "scanned.pdf"
    _make_pdf(str(pdf_path))  # no text layer - like a scanned page

    text = svc.extract_text(str(pdf_path))
    assert svc.has_extractable_text(text) is False  # confirms this is the case the fallback exists for

    image_bytes = svc.render_pdf_page_to_image(str(pdf_path))
    assert isinstance(image_bytes, bytes)
    assert len(image_bytes) > 0
    assert image_bytes[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG file signature, not just non-empty bytes
