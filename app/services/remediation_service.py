"""
Follow-up / Remediation Agent - the blueprint's own 15th workflow stage
("after the report, auditors verify whether management actually fixed
the identified problems... tracked until evidence supports closure").
Deliberately NOT a new record type: it operates on AuditFinding rows
already produced by the Finding Assistant, the same way PBC tracking
(Phase 4) already proved out for client document requests. This is
that exact same pattern - "overdue" computed fresh every time, never
stored, one narrow LLM call to draft a reminder - pointed at a
different kind of record (a finding needing remediation, not a
document request).

"Owner" and "target date" (this file's own vocabulary, matching the
blueprint's "Issue owner, Target date") are set by a human via a named
action, never inferred or assigned by an agent - the blueprint's own
permission matrix says a Follow-up Agent may "track due dates and
draft reminders," nothing about assigning them.
"""

import logging
from dataclasses import dataclass
from datetime import date

from app.services.llm_client import LLMUnavailableError, chat

logger = logging.getLogger(__name__)


@dataclass
class FindingLike:
    id: int
    title: str
    owner: str | None
    target_date: date | None
    status: str  # "open" / "resolved" / "dismissed"


@dataclass
class OverdueFinding:
    id: int
    title: str
    owner: str | None
    target_date: date
    days_overdue: int


def is_overdue(finding: FindingLike, today: date) -> bool:
    """A finding is overdue only once it has BOTH an owner and a target
    date assigned - one with no target date yet was simply never put on
    a timeline, which is a different situation from missing one it was
    actually given."""
    return finding.status == "open" and finding.target_date is not None and finding.target_date < today


def find_overdue(findings: list[FindingLike], today: date) -> list[OverdueFinding]:
    return [
        OverdueFinding(id=f.id, title=f.title, owner=f.owner, target_date=f.target_date, days_overdue=(today - f.target_date).days)
        for f in findings
        if is_overdue(f, today)
    ]


SYSTEM_PROMPT = (
    "You draft a short, professional follow-up email on behalf of an "
    "auditor, reminding the responsible owner(s) of open audit findings "
    "whose remediation is now overdue. Use ONLY the items listed below - "
    "never invent a finding, an owner, a date, or a number of days that "
    "isn't given to you. List each overdue finding by title, its owner, "
    "and how many days overdue it is. Keep a professional, constructive "
    "tone - this is a routine reminder, not an escalation or accusation. "
    "Output ONLY the email body - no subject line, no 'Dear ...' "
    "greeting or sign-off with a placeholder name, since the auditor "
    "will add those themselves before sending."
)


def _format_overdue_for_prompt(client_name: str, engagement_name: str, overdue_findings: list[OverdueFinding]) -> str:
    lines = [f"Client: {client_name}. Engagement: {engagement_name}. Overdue findings:"]
    for f in overdue_findings:
        owner = f.owner or "(no owner assigned)"
        lines.append(f"  - {f.title} - owner: {owner} - {f.days_overdue} day(s) overdue (target was {f.target_date.isoformat()})")
    return "\n".join(lines)


def draft_remediation_reminder(client_name: str, engagement_name: str, overdue_findings: list[OverdueFinding]) -> str:
    try:
        return chat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _format_overdue_for_prompt(client_name, engagement_name, overdue_findings)},
            ],
        ).strip()
    except LLMUnavailableError as e:
        logger.warning("Remediation reminder drafting failed: %s", e)
        raise
