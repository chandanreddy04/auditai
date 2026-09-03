# AuditAI

A multi-agent AI platform for an auditing firm reviewing several client
companies at once. Not one AI trying to "be the auditor" — a small team
of narrow, specialized AI coworkers, each responsible for one
measurable task, with a human auditor reviewing and approving
everything that actually matters.

This repo is **Phase 1 only**, deliberately. The full design calls for
a dozen specialist agents (document intake, transaction testing,
fraud-risk detection, controls testing, workpaper drafting, and more —
see the requirements this was built from). Phase 1 builds none of
those yet. It builds one narrow, reliable, explainable workflow first,
proves it out, and only then earns the right to add more:

```
document intake  →  AI extraction  →  reconciliation  →  exception detection  →  human review
```

## Why it's built this way

Three rules, non-negotiable from the first commit:

1. **The AI extracts, it never decides.** Reading a PDF and pulling out
   a vendor name, an amount, a reference number — that's language
   understanding, so an LLM does it
   ([`evidence_extraction_service.py`](app/services/evidence_extraction_service.py)).
   Deciding whether two documents actually reconcile, whether an amount
   mismatch is worth flagging — that's a plain comparison a human
   auditor could redo by hand, so it's plain Python, not a model call
   ([`reconciliation_service.py`](app/services/reconciliation_service.py)).
   If a human couldn't explain *why* something happened by pointing at
   a rule, it doesn't happen automatically.
2. **A human closes every exception.** The reconciliation engine can
   flag a mismatch; it can never resolve or dismiss one. That's a
   button only a named person can click, and every click is logged.
3. **Client data cannot leak, structurally.** Every table below the
   client level carries `client_id` and `engagement_id` directly, not
   just reachable through a join — a query filtered by client is one
   `WHERE` clause away, never something that could be forgotten. See
   the docstring at the top of [`app/models/models.py`](app/models/models.py).

## What Phase 1 actually does

- Create clients and engagements (one audit project per client).
- Upload evidence documents (purchase orders, invoices, payment
  records) as PDFs.
- Extract structured fields from each one with a local LLM
  (vendor, reference number, the *other* document's reference number
  it points back to, amount, date) — schema-constrained output, same
  technique proven out in this stack's sibling invoicing project.
- Automatically chain related documents together by reference number
  (PO → invoice → payment) and flag anything that doesn't chain
  cleanly: a missing reference, a reference that points to a document
  that was never uploaded, an amount that doesn't match within
  tolerance, or a duplicate reference number.
- Queue every flag for a human to resolve or dismiss, with a note.
- Log every agent action and every human decision to an append-only
  audit trail, per engagement.

## What Phase 1 deliberately does NOT do yet

- **No scanned/photographed evidence.** Only PDFs with a real text
  layer. Vision-based extraction for scans is a known, straightforward
  fast-follow (the technique is already proven elsewhere in this
  stack) — left out here to keep the first slice narrow.
- **No fraud/anomaly scoring, no controls testing, no workpaper
  drafting, no PBC/request tracking, no RAG policy lookup.** All real,
  all planned, all later phases — see the requirements doc this was
  scoped from.
- **No real authentication.** "Resolved by" is a typed name, not a
  login. Fine for proving the workflow; not fine for anything handling
  real client data.
- **No multi-agent orchestration.** There's exactly one hand-off in
  this phase (extraction → reconciliation), not a team of agents
  coordinating — that's Phase 5 of the roadmap, only once everything
  before it is trustworthy on its own.

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

`test_reconciliation_service.py` covers the actual decision engine with
zero LLM calls — pure input/output, deterministic, fast. `test_api.py`
exercises the full route flow end to end against a throwaway SQLite
database. `test_evidence_extraction_service.py` covers the extraction
service's schema handling and failure path with the LLM call mocked.

## Project layout

```
app/
  core/config.py                     settings, one seam for the environment
  models/models.py                   Client, Engagement, Document, EvidenceRecord,
                                      ReconciliationException, AuditLogEntry
  database/session.py                engine, session factory, get_db()
  schemas/extraction.py              the schema the LLM's output is constrained to
  services/
    llm_client.py                    Ollama/Groq switch, the only LLM seam
    pdf_text_service.py              deterministic PDF text-layer extraction
    evidence_extraction_service.py   LLM: document text -> structured evidence
    reconciliation_service.py        deterministic: the actual decision engine
    audit_log_service.py             one function, called after every action
  web/routes.py                      the whole Phase 1 flow, wired together
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
phases."* This is that first phase.
