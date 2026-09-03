# AuditAI

A multi-agent AI platform for an auditing firm reviewing several client
companies at once. Not one AI trying to "be the auditor" — a small team
of narrow, specialized AI coworkers, each responsible for one
measurable task, with a human auditor reviewing and approving
everything that actually matters.

The full design calls for a dozen specialist agents (document intake,
transaction testing, fraud-risk detection, controls testing, workpaper
drafting, and more — see the requirements this was built from). This
repo is built **one phase at a time**, per that requirements doc's own
recommendation: prove one narrow, reliable, explainable workflow before
adding the next.

```
Phase 1: document intake  →  AI extraction  →  reconciliation  →  exception detection  →  human review   [done]
Phase 2: controls testing (policy rules tested against the same evidence)                                [done]
Phase 3: workpaper drafting                                                                               [next]
Phase 4: PBC / request tracking
Phase 5: multi-agent orchestration across all of the above
```

## Why it's built this way

Four rules, non-negotiable from the first commit:

1. **The AI extracts, it never decides.** Reading a PDF and pulling out
   a vendor name, an amount, a reference number, an approver's name —
   that's language understanding, so an LLM does it
   ([`evidence_extraction_service.py`](app/services/evidence_extraction_service.py)).
   Deciding whether two documents reconcile, or whether a control is
   satisfied — that's a plain comparison a human auditor could redo by
   hand, so it's plain Python, not a model call
   ([`reconciliation_service.py`](app/services/reconciliation_service.py),
   [`controls_testing_service.py`](app/services/controls_testing_service.py)).
   If a human couldn't explain *why* something happened by pointing at
   a rule, it doesn't happen automatically.
2. **A human closes every finding.** Both engines can flag something;
   neither can resolve or dismiss it. That's a button only a named
   person can click, and every click is logged.
3. **Client data cannot leak, structurally.** Every table below the
   client level carries `client_id` and `engagement_id` directly, not
   just reachable through a join — a query filtered by client is one
   `WHERE` clause away, never something that could be forgotten. See
   the docstring at the top of [`app/models/models.py`](app/models/models.py).
4. **A human authors the rules, code just applies them consistently.**
   An auditor defines a control ("invoices over $1,000 require a PO")
   through the UI; the engine tests every piece of evidence against it
   the same way, every time. The LLM is never asked to judge compliance.

## What's built

**Phase 1 — reconciliation.** Upload evidence (purchase orders,
invoices, payment records) as PDFs. An LLM extracts structured fields
from each (vendor, reference number, the *other* document's reference
number it points back to, amount, date, approver) — schema-constrained
output, same technique proven out in this stack's sibling invoicing
project. A deterministic engine then chains related documents together
by reference number (PO → invoice → payment) and flags anything that
doesn't chain cleanly: a missing reference, a reference to a document
that was never uploaded, an amount that doesn't match within tolerance,
or a duplicate reference number.

**Phase 2 — controls testing.** An auditor defines internal controls to
test this engagement against, each with a dollar threshold — e.g. "POs
required over $1,000" or "approval required over $5,000." A second,
independent deterministic engine
([`controls_testing_service.py`](app/services/controls_testing_service.py))
tests every piece of evidence against every active control. A pass is
logged and closed automatically; a fail lands in the same kind of
review queue as a reconciliation exception, closed only by a named
human. This is deliberately a *different* kind of check than
reconciliation: reconciliation is unconditional ("every invoice should
chain to a PO"), controls testing is policy-driven and threshold-gated
("only *above this amount* does a PO become required") — the same
evidence can pass one and fail the other, or vice versa, and both are
tracked separately.

Both phases share: an append-only audit log of every agent action and
every human decision, and a human review queue where nothing auto-closes.

## Known limitations (found via live testing, not yet fixed)

- **Reconciliation compares invoice-to-PO amounts 1:1.** A single
  purchase order legitimately billed across multiple invoices (partial
  shipments, phased delivery) will show a spurious `amount_mismatch` on
  every invoice after the first, because the engine compares each
  invoice's amount directly against the PO's total rather than summing
  invoices per PO. Found live while testing Phase 2 (a second invoice
  against an already-matched PO triggered this). Real limitation, not
  silently patched — aggregating invoices per PO is a natural next
  improvement to `reconciliation_service.py`, not done yet.
- **No scanned/photographed evidence.** Only PDFs with a real text
  layer. Vision-based extraction for scans is a known, straightforward
  fast-follow (the technique is already proven elsewhere in this
  stack) — left out here to keep each phase narrow.
- **No real authentication.** "Resolved by" is a typed name, not a
  login. Fine for proving the workflow; not fine for anything handling
  real client data.
- **No multi-agent orchestration yet.** Each phase is its own
  independent deterministic engine triggered after upload - not a team
  of agents coordinating with each other. That's Phase 5, only once
  everything before it is trustworthy on its own.
- **Controls are defined per-engagement, not as a reusable client
  policy library.** A control an auditor defines on one engagement
  doesn't carry over to next year's engagement for the same client yet.

## Running it locally

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements.txt
ollama pull phi3.5            # one-time, for the local LLM backend
uvicorn app.main:app --reload
```

Open `http://localhost:8000`. No `GROQ_API_KEY` needed locally — it
talks to Ollama on localhost automatically. Setting `GROQ_API_KEY`
switches every LLM call to Groq's hosted free tier instead (see
[`app/services/llm_client.py`](app/services/llm_client.py)), for a
deployment target with no local GPU/CPU budget to run even a small
model — same dual-backend pattern already load-tested elsewhere in
this stack.

## Tests

```bash
pytest
```

`test_reconciliation_service.py` and `test_controls_testing_service.py`
cover the two decision engines with zero LLM calls — pure input/output,
deterministic, fast. `test_api.py` exercises the full route flow
(clients, engagements, uploads, exceptions, controls) end to end
against a throwaway SQLite database. `test_evidence_extraction_service.py`
covers the extraction service's schema handling, normalization, and
failure path with the LLM call mocked.

Every feature here has also been verified **live**, not just against
mocks: a real local model (phi3.5 via Ollama), a real running server,
real generated PDFs uploaded through the actual routes and browser UI.
Two real extraction bugs were found and fixed this way (see git log) —
mocked tests alone would not have caught either.

## Project layout

```
app/
  core/config.py                     settings, one seam for the environment
  models/models.py                   Client, Engagement, Document, EvidenceRecord,
                                      ReconciliationException, Control, ControlTestResult,
                                      AuditLogEntry
  database/session.py                engine, session factory, get_db()
  schemas/extraction.py              the schema the LLM's output is constrained to
  services/
    llm_client.py                    Ollama/Groq switch, the only LLM seam
    pdf_text_service.py              deterministic PDF text-layer extraction
    evidence_extraction_service.py   LLM: document text -> structured evidence
    reconciliation_service.py        deterministic: Phase 1's decision engine
    controls_testing_service.py      deterministic: Phase 2's decision engine
    audit_log_service.py             one function, called after every action
  web/routes.py                      the whole app, wired together
tests/
```

## Where this came from

Scoped directly from an internal requirements document written for an
auditing firm auditing 4-5 client companies that wanted AI coworkers
to help with document-heavy audit work, while keeping every
consequential judgment with a licensed human auditor. That document's
own recommendation was followed literally: *"do not begin by building
ten autonomous agents — start with one narrow, measurable workflow and
make it reliable, secure, and explainable, then add capabilities in
phases."* This repo is that recommendation, built one phase at a time.
