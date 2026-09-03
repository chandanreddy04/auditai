"""
Phase 4 - tracking, mostly, plus one narrow LLM draft at the end.

"Overdue" is a plain date comparison (today vs. a due date) - a human
could work this out by eye from the list, so it's plain Python here,
computed fresh every time rather than stored and drifting out of date.
Nothing in this file ever marks an item received or waived; that stays
a named human action in routes.py, same discipline as every other
closure in this app.

The one LLM call, draft_reminder_email(), follows the same boundary as
workpaper_service.py's drafting call: it is handed only the already-
computed list of overdue items (never asked to decide what's overdue,
never shown anything else about the engagement) and asked to turn that
into a polite follow-up email a human can review and send. Sending it
is not something this app does - see the project-wide rule that
sending any message on a human's behalf needs an explicit human action,
which "drafting text for a human to copy" deliberately stays short of.
"""

import logging
from dataclasses import dataclass
from datetime import date

from app.services.llm_client import LLMUnavailableError, chat

logger = logging.getLogger(__name__)


@dataclass
class PBCLike:
    id: int
    item_name: str
    due_date: date | None
    status: str  # "requested" / "received" / "waived"


@dataclass
class OverdueItem:
    id: int
    item_name: str
    due_date: date
    days_overdue: int


def is_overdue(item: PBCLike, today: date) -> bool:
    return item.status == "requested" and item.due_date is not None and item.due_date < today


def find_overdue(items: list[PBCLike], today: date) -> list[OverdueItem]:
    return [
        OverdueItem(id=item.id, item_name=item.item_name, due_date=item.due_date, days_overdue=(today - item.due_date).days)
        for item in items
        if is_overdue(item, today)
    ]


SYSTEM_PROMPT = (
    "You draft a short, polite follow-up email to a client on behalf of "
    "an auditor, reminding them of outstanding document requests that "
    "are now overdue. Use ONLY the items listed below - never invent an "
    "item, a date, or a number of days that isn't given to you. List "
    "each overdue item by name with how many days overdue it is. Keep "
    "a professional, courteous tone - this is a routine reminder, not "
    "an escalation. Output ONLY the email body - no subject line, no "
    "'Dear ...' greeting or sign-off with a placeholder name, since the "
    "auditor will add those themselves before sending."
)


def _format_overdue_for_prompt(client_name: str, engagement_name: str, overdue_items: list[OverdueItem]) -> str:
    lines = [f"Client: {client_name}. Engagement: {engagement_name}. Overdue items:"]
    for item in overdue_items:
        lines.append(f"  - {item.item_name}: {item.days_overdue} day(s) overdue (was due {item.due_date.isoformat()})")
    return "\n".join(lines)


def draft_reminder_email(client_name: str, engagement_name: str, overdue_items: list[OverdueItem]) -> str:
    try:
        return chat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _format_overdue_for_prompt(client_name, engagement_name, overdue_items)},
            ],
        ).strip()
    except LLMUnavailableError as e:
        logger.warning("PBC reminder drafting failed: %s", e)
        raise
