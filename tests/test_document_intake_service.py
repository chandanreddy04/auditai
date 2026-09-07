from app.models.models import AuditArea, DocumentType
from app.services import document_intake_service as svc


# --------------------------------------------------------- classify_file_kind

def test_classify_file_kind_pdf():
    assert svc.classify_file_kind("invoice.pdf") == "pdf"
    assert svc.classify_file_kind("INVOICE.PDF") == "pdf"


def test_classify_file_kind_image():
    assert svc.classify_file_kind("photo.jpg") == "image"
    assert svc.classify_file_kind("photo.jpeg") == "image"
    assert svc.classify_file_kind("scan.png") == "image"


def test_classify_file_kind_unknown():
    assert svc.classify_file_kind("notes.txt") == "unknown"
    assert svc.classify_file_kind("") == "unknown"
    assert svc.classify_file_kind(None) == "unknown"


# --------------------------------------------------------- route_to_audit_area

def test_route_to_audit_area_none_is_uncategorized():
    assert svc.route_to_audit_area(None) == AuditArea.UNCATEGORIZED


def test_route_to_audit_area_vendor_payments():
    assert svc.route_to_audit_area(DocumentType.PURCHASE_ORDER) == AuditArea.VENDOR_PAYMENTS
    assert svc.route_to_audit_area(DocumentType.INVOICE) == AuditArea.VENDOR_PAYMENTS
    assert svc.route_to_audit_area(DocumentType.PAYMENT) == AuditArea.VENDOR_PAYMENTS


def test_route_to_audit_area_banking_cash():
    assert svc.route_to_audit_area(DocumentType.BANK_STATEMENT) == AuditArea.BANKING_CASH


def test_route_to_audit_area_unknown_doc_type_is_uncategorized():
    assert svc.route_to_audit_area(DocumentType.UNKNOWN) == AuditArea.UNCATEGORIZED
