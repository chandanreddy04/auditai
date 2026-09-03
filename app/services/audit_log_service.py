"""
One function every route/service calls after any action worth
remembering - document uploaded, evidence extracted, reconciliation
run, exception resolved by a human. Deliberately trivial: the value of
an audit trail is that writing to it is never optional or forgotten,
not that the mechanism is clever.
"""

from sqlalchemy.orm import Session

from app.models.models import AuditLogEntry


def log(
    db: Session,
    actor: str,
    action: str,
    detail: str = "",
    engagement_id: int | None = None,
    client_id: int | None = None,
) -> None:
    entry = AuditLogEntry(
        actor=actor,
        action=action,
        detail=detail,
        engagement_id=engagement_id,
        client_id=client_id,
    )
    db.add(entry)
    db.commit()
