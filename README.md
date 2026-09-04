# AuditAI

A multi-agent AI platform for an auditing firm reviewing several client
companies at once. Not one AI trying to "be the auditor" — a small team
of narrow, specialized AI coworkers, each responsible for one
measurable task, with a human auditor reviewing and approving
everything that actually matters.

The full design calls for a dozen specialist agents (document intake,
transaction testing, fraud-risk detection, controls testing, workpaper
drafting, and more — see the requirements this was built from). This
repo was built **one phase at a time**, per that requirements doc's own
recommendation: prove one narrow, reliable, explainable workflow before
adding the next. All five planned phases are now built.

```
Phase 1: document intake  →  AI extraction  →  reconciliation  →  exception detection  →  human review   [done]
Phase 2: controls testing (policy rules tested against the same evidence)                                [done]
Phase 3: workpaper drafting                                                                               [done]
Phase 4: PBC / request tracking                                                                           [done]
Phase 5: multi-agent orchestration across all of the above                                                [done]
```

Beyond that original 5-phase roadmap, three more pieces have since been
added: **real user accounts** (signup/login/logout) replacing the
typed-name fields the five phases originally used for "who did this";
**vision extraction** for scanned documents and photos; and the
**Anomaly / Fraud-Risk Detection Agent** below. A later, more detailed
revision of the requirements document named 12 specific agents in
total (not just the 5 phases) - as of the fraud-risk agent, **5 of
those 12 are built**: Evidence Extraction, Reconciliation, Controls
Testing, Workpaper Drafting, and now Anomaly/Fraud-Risk Detection. The
other 7 (Transaction Testing, Policy/Knowledge (RAG), Audit Finding
Assistant, Follow-up/Remediation, Auditor Research Copilot, and full
Document Intake / PBC lifecycles) are not built yet.

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
invoices, payment records) as PDFs, photos, or screenshots. A PDF with
a real text layer is read directly; a scanned PDF page or a JPG/PNG
photo has no text layer at all, so it's handed to a vision-capable
model instead — same schema, same output shape either way, same
technique already proven out in this stack's sibling invoicing
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

**Phase 3 — workpaper drafting.** A deterministic summary builder
([`workpaper_service.py`](app/services/workpaper_service.py)) gathers
everything already decided in Phases 1 and 2 for an engagement —
document/evidence counts, every reconciliation exception and how it
was resolved, every control result and its resolution note — into a
structured summary. An LLM is then handed *only* that summary, never
raw evidence, and asked to write it up as a short professional memo.
The draft is fully editable by a human before finalizing; finalizing
is the one irreversible action in this phase and is logged like every
other consequential action. Live-tested this generates an accurate,
fact-checkable memo — every number and finding in the draft traced
back to a real row in the database, nothing invented.

**Phase 4 — PBC (Provided-By-Client) tracking.** The most standalone
phase so far — doesn't require any evidence to exist first. An auditor
logs requests sent to the client (a document, a schedule, an
explanation), each with an optional due date. "Overdue" is never
stored; it's computed fresh every time from today vs. the due date
([`pbc_service.py`](app/services/pbc_service.py)), so it can never
drift out of date the way a stored status could. A human marks an item
received (optionally linking it to a Document actually uploaded to
satisfy it) or waived. The one LLM touch in this phase: a button drafts
a polite reminder email listing only the already-computed overdue
items and how many days overdue each is — the auditor copies, reviews,
and sends it themselves; this app never sends mail on anyone's behalf.

**Phase 5 — multi-agent orchestration.** Before this phase, uploading a
document quietly called three functions in a row inside a route
handler — extraction, reconciliation, controls testing — with no
record beyond scattered audit-log prose lines that it happened in that
order. [`orchestration_service.py`](app/services/orchestration_service.py)
makes that sequencing explicit: every upload creates a persisted
`OrchestrationRun` with an ordered `OrchestrationStep` per agent - which
one ran, in what order, with what result, how long it took. If
extraction fails or finds nothing to extract, the downstream
reconciliation and controls-testing steps are recorded as **SKIPPED**
with a reason, not silently absent — the full intended pipeline is
always visible, never just the steps that happened to execute. A
second, human-triggered entry point (`/engagements/{id}/orchestration`,
"Run full check now") re-runs reconciliation and controls testing
across all existing evidence on demand — useful right after defining a
new control, without re-uploading anything. This adds no new judgment
anywhere: every agent it calls already existed and already made its
own decisions in Phases 1-2; orchestration only coordinates and writes
down that it did, closing the loop on the blueprint's own architecture
diagram (Human Auditor → Orchestrator → testing agents → Human Review).

All five phases share: an append-only audit log of every agent action
and every human decision, and a human review queue where nothing
auto-closes on its own.

**Real authentication (new scope beyond the 5-phase roadmap).** Every
route in the app now requires a logged-in user
([`auth_routes.py`](app/web/auth_routes.py)) - session-based, via a
signed cookie (Starlette's `SessionMiddleware`), not JWTs or OAuth this
single-server app doesn't need. Passwords are hashed with PBKDF2-HMAC-
SHA256 from the standard library, not bcrypt/argon2 - see
[`auth_service.py`](app/services/auth_service.py) for why that
trade-off makes sense here. Every "your name" text field that used to
appear on a Resolve/Finalize/Waive/Run-full-check form is gone -
`current_user.name` fills it in automatically, so a resolution can no
longer be attributed to a name someone else typed. Historical records
created before this feature still show generic actors like `"human"`
- an honest gap, not rewritten after the fact.

**Anomaly / Fraud-Risk Detection Agent.** A third independent
deterministic engine ([`fraud_risk_service.py`](app/services/fraud_risk_service.py)),
same discipline as reconciliation and controls testing - zero LLM
calls, plain pattern-matching over evidence already on file. The
blueprint is explicit that this agent "should not automatically
declare fraud," and this project's own history backs that up hard: a
reasoning-model-based fraud feature in the sibling InvoiceIQ project
was built, tested, and reverted twice for being too slow and too
unreliable for exactly this kind of judgment call. Four heuristics
run automatically after every upload (and on a manual "run full
check"): a vendor billed the same amount twice under different
reference numbers (duplicate payment risk), a suspiciously round total,
a weekend-dated document, and a vendor's only appearance in the
engagement above a materiality threshold. Every flag is a prompt for a
human to look closer, never a conclusion, and sits in the same kind of
review queue as every other flag in this app.

## Known limitations (found via live testing, not yet fixed)

- **Groq's free tier has a daily request quota (1,000/day), separate
  from its per-minute rate limit.** Found live: after a day of heavy
  testing (deployment checks, vision extraction, fraud-risk agent),
  the deployed site started returning `429 Rate limit reached ...
  RPD: Limit 1000, Used 1000` on every AI extraction call - the
  service itself was healthy and responding (`/health` returned 200
  the whole time), only the LLM call was blocked. Same
  `LLMUnavailableError` handling as any other LLM outage: the step
  fails cleanly, downstream steps are correctly skipped, nothing
  crashes or corrupts data. The quota resets on Groq's own schedule;
  there's no in-app workaround (a paid Groq tier, or switching back to
  local Ollama, are the two real options if this needs to not happen).
- **A fraud-risk flag's wording can go stale.** Flags are never
  auto-retracted, same "only a human closes it" discipline as every
  other flag in this app - but that means if a "this vendor only
  appears once" flag was raised, then a *second* document for that
  same vendor arrives later, the original flag stays open with its
  now-slightly-inaccurate wording rather than being reconsidered.
  Found live: uploading two invoices for the same new vendor in
  sequence left an earlier "appears only once" flag open after a
  second one made that literally no longer true. The underlying
  concern (a human should glance at this vendor) is still reasonable
  to keep open; the description text just doesn't update itself.

- **Reconciliation compares invoice-to-PO amounts 1:1.** A single
  purchase order legitimately billed across multiple invoices (partial
  shipments, phased delivery) will show a spurious `amount_mismatch` on
  every invoice after the first, because the engine compares each
  invoice's amount directly against the PO's total rather than summing
  invoices per PO. Found live while testing Phase 2 (a second invoice
  against an already-matched PO triggered this). Real limitation, not
  silently patched — aggregating invoices per PO is a natural next
  improvement to `reconciliation_service.py`, not done yet.
- **Vision extraction accuracy varies a lot by model.** Tested live: a
  rendered scanned invoice fed to the local Ollama vision model
  (`llava-phi3`, small, older) came back with a wrong doc_type, a
  fabricated reference number, and a missed amount - genuinely poor
  reading, not a bug in the surrounding code (confirmed by inspecting
  the model's raw output directly). The same document against Groq's
  larger hosted vision model (used automatically once `GROQ_API_KEY`
  is set, e.g. on the deployed site) should be meaningfully more
  reliable, but "meaningfully more reliable" still isn't "trustworthy
  without a human checking it" - treat any vision-extracted evidence
  record as a rough first draft a human should double-check against
  the actual image, more so than text-extracted evidence.
- **Authentication has no email verification, password reset, or
  login-attempt rate limiting.** Any email/password gets an account
  immediately; nothing stops repeated login guesses. Fine for a small
  internal tool proving the workflow; a real deployment handling real
  client data needs all three before going further.
- **No roles or permissions.** Every logged-in user can do everything -
  there's no "partner vs. staff" distinction yet, even though the
  blueprint's own permission matrix calls for one.
- **The orchestrator only coordinates the automatic per-upload pipeline
  and the manual full-check re-run.** Workpaper drafting and PBC
  reminders are deliberately NOT part of any orchestrated pipeline -
  they stay human-triggered "when I'm ready" actions (see their own
  phase notes above), so they don't appear in the orchestration
  history. A more complete orchestration layer might let an auditor
  compose a custom pipeline across all five phases; this one runs the
  two pipelines that were actually useful to automate.
- **No background job queue.** An OrchestrationRun is always
  synchronous, start to finish, within one HTTP request - there is no
  "in progress" status, and a slow LLM step (see the note below) means
  the browser waits for the whole request to complete.
- **Controls are defined per-engagement, not as a reusable client
  policy library.** A control an auditor defines on one engagement
  doesn't carry over to next year's engagement for the same client yet.
- **One workpaper draft per engagement, no version history.** Every
  "Generate draft" overwrites the current draft's content (only ever a
  concern before finalizing - finalized workpapers are locked). A real
  version history is a natural improvement, not built yet.
- **Free-form drafting calls (the workpaper memo, the PBC reminder
  email) are noticeably slower than the schema-constrained extraction
  calls** on CPU-only local Ollama — observed 1-3+ minutes for a
  several-paragraph draft, versus a few seconds for structured
  extraction. Ollama also serializes requests, so submitting a second
  draft before the first finishes queues behind it rather than running
  in parallel. Not a code bug - a real characteristic of local CPU
  inference worth knowing about if a request appears to hang.

## Running it locally

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements.txt
ollama pull phi3.5            # one-time, for the local LLM backend
uvicorn app.main:app --reload
```

Open `http://localhost:8000` - it redirects to `/login`; sign up for
an account on first visit. No `GROQ_API_KEY` needed locally — it
talks to Ollama on localhost automatically. Setting `GROQ_API_KEY`
switches every LLM call to Groq's hosted free tier instead (see
[`app/services/llm_client.py`](app/services/llm_client.py)), for a
deployment target with no local GPU/CPU budget to run even a small
model — same dual-backend pattern already load-tested elsewhere in
this stack.

Set `SECRET_KEY` in the environment for anything beyond local dev - it
signs the session cookie, and the fallback value in `config.py` is
intentionally insecure (anyone who reads the source could forge a
session with it).

## Deploying it

[`render.yaml`](render.yaml) + [`Dockerfile`](Dockerfile) deploy this
app as a web service on [Render](https://render.com): New + → Blueprint
→ connect this repo → Render reads `render.yaml` automatically.
`SECRET_KEY` is generated for you automatically.

**Database - a deliberate deviation from InvoiceIQ's Blueprint.**
Render's free tier allows only one free Postgres instance per account,
and InvoiceIQ (this app's sibling project, deployed on the same
account) already has it. Rather than pay for a second instance,
`render.yaml` does NOT declare its own database - it reuses InvoiceIQ's
existing free Postgres *server* with a second, completely separate
`auditai` database created on it. Real data isolation (a totally
different database, not a shared schema - InvoiceIQ and AuditAI even
both happen to have a `users` table, so sharing one database would
genuinely break things, not just be messy), zero extra cost. One-time
setup after the first deploy:

1. In the Render dashboard, open InvoiceIQ's Postgres instance → **Connect**
   → copy the **External Connection String**.
2. In your own terminal (not here - this connection string includes a
   password): `psql "<that connection string>" -c "CREATE DATABASE auditai;"`
3. Take that same connection string and change the database name at
   the end from `/invoiceiq` to `/auditai` - that's AuditAI's own
   `DATABASE_URL`.
4. In AuditAI's Render service → **Environment** tab, set `DATABASE_URL`
   to that value. Render redeploys automatically.

Until step 4 is done, the app falls back to a local SQLite file inside
the container and still starts and passes its health check - it just
won't persist data across restarts, since Render's free web services
have no persistent disk. Postgres via the steps above is what makes
data actually survive a redeploy.

The deployed app works fully (client/engagement management, uploads,
all five review queues, dashboards) with **no LLM at all** by default -
every AI-touching step falls back to its documented "LLM unavailable"
behavior rather than failing silently. To enable AI extraction,
workpaper drafting, and PBC reminders in the cloud, add a free
[Groq API key](https://console.groq.com/keys) as the `GROQ_API_KEY`
environment variable in the Render dashboard after the first deploy
(`sync: false` in `render.yaml` means Render prompts for it
interactively rather than expecting it committed to the repo) - see
[`app/services/llm_client.py`](app/services/llm_client.py) for the
backend switch this triggers. Use a **separate** Groq key from
InvoiceIQ's, not the same one - Groq's free-tier rate limit is per key,
so sharing one means heavy usage (or a bug) in either app can throttle
the other, and separate keys make usage/debugging independent in
Groq's own dashboard.

Verified locally before ever deploying: the `postgres://` → `postgresql://`
URL rewrite `config.py` needs for Render's Postgres connection strings,
and the app starting cleanly bound to `0.0.0.0` on a `PORT` environment
variable (exactly how the Dockerfile's `CMD` and Render's runtime both
invoke it) with a real production-style `SECRET_KEY` - not the Docker
build itself, since this machine doesn't have Docker installed.

## Tests

```bash
pytest
```

`test_reconciliation_service.py`, `test_controls_testing_service.py`,
`test_fraud_risk_service.py`, `test_pbc_service.py`, and the
summary-building half of `test_workpaper_service.py` cover the five
decision/summary engines with zero LLM calls — pure input/output,
deterministic, fast. `test_orchestration_service.py` covers the
coordination logic itself (step ordering, skip-on-failure, run-status
rollup) with the LLM-touching extraction call mocked. `test_api.py`
exercises the full route flow (clients, engagements, uploads,
exceptions, fraud-risk flags, controls, workpaper generate/edit/finalize,
PBC requests/receive/waive/reminder, orchestration runs/manual
full-check) end to end against a throwaway SQLite database.
`test_evidence_extraction_service.py` and the drafting halves of
`test_workpaper_service.py`/`test_pbc_service.py` cover LLM-touching
code with the model call mocked.

`test_auth_service.py` covers password hashing (zero LLM, zero HTTP).
`test_auth_routes.py` covers signup/login/logout and the
redirect-when-logged-out behavior with its own `TestClient`, since
`test_api.py` shares one already-logged-in client across its whole file.

`test_pdf_text_service.py` covers the text/image gate (`has_extractable_text`)
and the real PDF-to-PNG render (`render_pdf_page_to_image`) with actual
PyMuPDF calls, no mocking. The vision half of
`test_evidence_extraction_service.py` and three new cases in
`test_orchestration_service.py` (scanned PDF succeeds via vision,
scanned PDF fails when the vision model is unavailable, a direct image
upload never touches the PDF text path at all) cover the vision
extraction route with the model call mocked.

Every feature here has also been verified **live**, not just against
mocks: a real local model (phi3.5 via Ollama), a real running server,
real generated PDFs uploaded through the actual routes and browser UI.
Two real extraction bugs and one prompt-formatting issue in the
workpaper drafter were found and fixed this way (see git log) — mocked
tests alone would not have caught any of them.

## Project layout

```
app/
  core/config.py                     settings, one seam for the environment
  models/models.py                   User, Client, Engagement, Document, EvidenceRecord,
                                      ReconciliationException, Control, ControlTestResult,
                                      Workpaper, PBCRequest, FraudRiskFlag,
                                      OrchestrationRun, OrchestrationStep, AuditLogEntry
  database/session.py                engine, session factory, get_db()
  schemas/extraction.py              the schema the LLM's output is constrained to
  services/
    auth_service.py                  password hashing (PBKDF2-HMAC-SHA256)
    llm_client.py                    Ollama/Groq switch (text + vision), the only LLM seam
    pdf_text_service.py              deterministic PDF text extraction + PDF-to-image render
    evidence_extraction_service.py   LLM (text or vision): document -> structured evidence
    reconciliation_service.py        deterministic: Phase 1's decision engine
    controls_testing_service.py      deterministic: Phase 2's decision engine
    fraud_risk_service.py            deterministic: pattern-based risk-flag engine, zero LLM
    workpaper_service.py             deterministic summary builder + Phase 3's one LLM call
    pbc_service.py                   deterministic overdue calc + Phase 4's one LLM call
    orchestration_service.py         Phase 5: coordinates the agents above, logs every step
    audit_log_service.py             one function, called after every action
  web/
    auth_routes.py                   signup/login/logout + get_current_user dependency
    routes.py                        the whole app, wired together
tests/
Dockerfile                            container image, used both locally and by render.yaml
render.yaml                           Render Blueprint: web service + free Postgres, one click
```

## Where this came from

Scoped directly from an internal requirements document written for an
auditing firm auditing 4-5 client companies that wanted AI coworkers
to help with document-heavy audit work, while keeping every
consequential judgment with a licensed human auditor. That document's
own recommendation was followed literally: *"do not begin by building
ten autonomous agents — start with one narrow, measurable workflow and
make it reliable, secure, and explainable, then add capabilities in
phases."* This repo is that recommendation, built one phase at a time -
all five planned phases now complete, plus real authentication, vision
extraction, and the Anomaly/Fraud-Risk Detection Agent added as
follow-ups, 120 tests passing, every feature verified live against a
real model, not just against mocks.
