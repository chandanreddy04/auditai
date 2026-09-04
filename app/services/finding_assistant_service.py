"""
The Audit Finding Assistant, per the blueprint's Section 7 spec: turn
an already-detected issue into a structured finding - title, risk
rating, root cause, impact, recommendation. Same two-layer split as
workpaper_service.py, and the same "the model narrates, code decides"
boundary used everywhere in this app:

- gather_finding_candidates(): plain Python, zero LLM. Decides WHICH
  items are worth a finding (any open or resolved - never dismissed -
  ReconciliationException, failed ControlTestResult, or FraudRiskFlag
  that doesn't already have a finding written up for it) and assigns
  risk_rating from a fixed lookup table. A finding's risk rating is a
  judgment with real consequences for what an auditor prioritizes -
  exactly the kind of call this project has never handed to an LLM,
  and reconciliation/controls/fraud-risk's own engines already encode
  the actual severity signal (exception type, pass/fail, or fraud
  flag's own severity) more reliably than asking a model to re-derive
  it from prose.
- draft_findings(): the one LLM call. Given ALL candidates' facts in
  one batch (title-worthy items are usually reviewed together, and
  batching means one slow call instead of one per finding), asked to
  write title/root_cause/impact/recommendation for each - never asked
  to invent a finding that wasn't in the candidate list, never asked to
  judge severity.

Dedup is by (source_type, source_id), the same "never reopen or
duplicate something already on file" pattern as every other engine in
this app - re-running after new exceptions appear only drafts findings
for the NEW candidates, never touches ones already written up.
"""

import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.models.models import FindingRiskRating, FindingSourceType
from app.services.llm_client import LLMUnavailableError, chat

logger = logging.getLogger(__name__)


@dataclass
class FindingCandidate:
    source_type: FindingSourceType
    source_id: int
    fact_summary: str
    risk_rating: FindingRiskRating

    @property
    def source_key(self) -> str:
        return f"{self.source_type.value}:{self.source_id}"


# Fixed lookup, not a model call - see module docstring for why. Each
# engine's own output already carries the real severity signal.
_EXCEPTION_TYPE_RISK = {
    "duplicate": FindingRiskRating.HIGH,
    "missing_match": FindingRiskRating.HIGH,
    "amount_mismatch": FindingRiskRating.MEDIUM,
    "unreadable": FindingRiskRating.LOW,
}
_FRAUD_SEVERITY_RISK = {
    "high": FindingRiskRating.HIGH,
    "medium": FindingRiskRating.MEDIUM,
    "low": FindingRiskRating.LOW,
}


def _exception_candidates(exceptions, existing_keys: set[tuple]) -> list[FindingCandidate]:
    candidates = []
    for e in exceptions:
        key = (FindingSourceType.RECONCILIATION_EXCEPTION.value, e.id)
        if key in existing_keys:
            continue
        risk = _EXCEPTION_TYPE_RISK.get(e.exception_type.value, FindingRiskRating.MEDIUM)
        note = f" Resolution on file: {e.resolved_by} - {e.resolution_note}" if e.resolution_note else ""
        candidates.append(FindingCandidate(
            source_type=FindingSourceType.RECONCILIATION_EXCEPTION, source_id=e.id,
            fact_summary=f"Reconciliation exception ({e.exception_type.value}, status={e.status.value}): "
                         f"{e.description}{note}",
            risk_rating=risk,
        ))
    return candidates


def _control_failure_candidates(control_results, existing_keys: set[tuple]) -> list[FindingCandidate]:
    candidates = []
    for r in control_results:
        if r.result.value != "fail":
            continue  # a pass is not a finding
        key = (FindingSourceType.CONTROL_FAILURE.value, r.id)
        if key in existing_keys:
            continue
        note = f" Resolution on file: {r.resolved_by} - {r.resolution_note}" if r.resolution_note else ""
        candidates.append(FindingCandidate(
            source_type=FindingSourceType.CONTROL_FAILURE, source_id=r.id,
            fact_summary=f"Failed control test ({r.control.name}, status={r.status.value}): {r.detail}{note}",
            risk_rating=FindingRiskRating.HIGH,
        ))
    return candidates


def _fraud_flag_candidates(fraud_flags, existing_keys: set[tuple]) -> list[FindingCandidate]:
    candidates = []
    for f in fraud_flags:
        key = (FindingSourceType.FRAUD_RISK_FLAG.value, f.id)
        if key in existing_keys:
            continue
        risk = _FRAUD_SEVERITY_RISK.get(f.severity, FindingRiskRating.MEDIUM)
        note = f" Resolution on file: {f.resolved_by} - {f.resolution_note}" if f.resolution_note else ""
        candidates.append(FindingCandidate(
            source_type=FindingSourceType.FRAUD_RISK_FLAG, source_id=f.id,
            fact_summary=f"Fraud-risk flag ({f.flag_type.value}, status={f.status.value}): {f.description}{note}",
            risk_rating=risk,
        ))
    return candidates


def gather_finding_candidates(exceptions, control_results, fraud_flags, existing_findings) -> list[FindingCandidate]:
    """Plain Python, zero LLM. Excludes anything DISMISSED (a human
    already decided it wasn't a real issue) and anything that already
    has a finding written up (dedup by source_type+source_id)."""
    non_dismissed_exceptions = [e for e in exceptions if e.status.value != "dismissed"]
    non_dismissed_flags = [f for f in fraud_flags if f.status.value != "dismissed"]
    existing_keys = {(f.source_type.value, f.source_id) for f in existing_findings}

    candidates: list[FindingCandidate] = []
    candidates.extend(_exception_candidates(non_dismissed_exceptions, existing_keys))
    candidates.extend(_control_failure_candidates(control_results, existing_keys))
    candidates.extend(_fraud_flag_candidates(non_dismissed_flags, existing_keys))
    return candidates


class LLMFindingDetail(BaseModel):
    source_key: str = Field(description="Copy this EXACT source_key from the item you are writing up - unchanged.")
    title: str = Field(description="A short finding title, under 12 words, e.g. 'Unapproved payment exceeding control threshold'.")
    root_cause: str = Field(description="1-2 sentences: why this happened, using ONLY the facts given for this item.")
    impact: str = Field(description="1-2 sentences: why this matters to the audit or the business.")
    recommendation: str = Field(description="1-2 sentences: what the auditee should do about it.")


class LLMFindingsResponse(BaseModel):
    findings: list[LLMFindingDetail]


SYSTEM_PROMPT = (
    "You are an audit finding assistant. You are given a numbered list "
    "of items, each with a source_key and the facts already established "
    "about it (an exception, a failed control test, or a fraud-risk "
    "flag). For EACH item, write a structured finding: a short title, "
    "a root cause (why this happened), an impact (why it matters), and "
    "a recommendation (what to do about it).\n\n"
    "Rules: write EXACTLY one finding per item given, copying its "
    "source_key exactly. Never skip an item. Never add a finding for "
    "an item that was not given to you. Never invent a fact, a number, "
    "or a name that is not in the item's own description - if the "
    "description doesn't say why something happened, say the root "
    "cause is not yet determined and recommend investigating it, rather "
    "than guessing. Do not assign or mention a severity or risk rating "
    "- that is decided separately."
)


def _format_candidates_for_prompt(candidates: list[FindingCandidate]) -> str:
    lines = []
    for i, c in enumerate(candidates, start=1):
        lines.append(f"{i}. source_key={c.source_key}\n   {c.fact_summary}")
    return "\n".join(lines)


def draft_findings(candidates: list[FindingCandidate]) -> list[LLMFindingDetail]:
    """The one LLM call: batch every candidate into a single request
    (one slow call instead of one per finding) and validate the reply
    covers exactly the given source_keys - same "don't trust the
    model's bookkeeping, only its writing" discipline as extraction's
    doc_type normalization. An item the model dropped or an extra key
    it invented is logged and filtered out rather than trusted blindly."""
    if not candidates:
        return []

    try:
        content = chat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _format_candidates_for_prompt(candidates)},
            ],
            schema=LLMFindingsResponse.model_json_schema(),
        )
        parsed = LLMFindingsResponse.model_validate_json(content)
    except LLMUnavailableError as e:
        logger.warning("Finding drafting failed: %s", e)
        raise
    except ValueError as e:
        logger.warning("Finding drafting returned invalid structured output: %s", e)
        raise LLMUnavailableError(f"Model output did not match schema: {e}") from e

    valid_keys = {c.source_key for c in candidates}
    results = [f for f in parsed.findings if f.source_key in valid_keys]
    if len(results) != len(candidates):
        found_keys = {f.source_key for f in results}
        missing = valid_keys - found_keys
        logger.warning("Finding drafting did not cover every candidate - missing: %s", missing)
    return results
