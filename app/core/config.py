"""
Central place every other module reads settings from - nothing else in
the app should call os.getenv() directly. Same reasoning as InvoiceIQ's
config.py: one seam, easy to see everything the app depends on from the
environment in one file.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'auditai.db'}")

# Render (and Heroku-style) managed Postgres hand out connection
# strings starting "postgres://" - the legacy scheme. SQLAlchemy 1.4+
# requires "postgresql://" and raises on the old one. Rewriting it
# here means the exact connection string a cloud provider gives you
# can be pasted into DATABASE_URL verbatim, no manual editing needed -
# same fix already proven necessary and applied in InvoiceIQ's config.py.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Local-first: Ollama on localhost, no key needed. Setting GROQ_API_KEY
# switches every LLM call in the app to Groq's hosted free tier instead -
# same dual-backend pattern already proven out and load-tested in
# InvoiceIQ, reused here rather than reinvented.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip() or None
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "phi3.5")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

# Vision-capable models - only used for scanned PDFs (no text layer) and
# directly-uploaded photos/screenshots (JPG/PNG). Same models already
# proven out in InvoiceIQ's own vision extraction.
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "llava-phi3")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")

# Reconciliation tolerance: two amounts that should match (e.g. an
# invoice total against its purchase order) are still considered a
# match if they differ by less than this - real documents round tax
# and shipping slightly differently. Anything beyond this becomes an
# amount_mismatch exception for a human to look at, never auto-resolved.
RECONCILIATION_AMOUNT_TOLERANCE = float(os.getenv("RECONCILIATION_AMOUNT_TOLERANCE", "1.00"))

UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Signs the session cookie (see app/web/auth_routes.py). The fallback is
# fine for local dev only - anyone who reads it could forge a session
# cookie, so a real deployment MUST set SECRET_KEY in the environment.
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-insecure-secret-key-change-me")
