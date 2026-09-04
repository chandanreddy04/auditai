"""
Password hashing only - session management lives in app/web/auth_routes.py
(a signed cookie via Starlette's SessionMiddleware, nothing this
single-server app needs a database session table or JWTs for).

PBKDF2-HMAC-SHA256 from the standard library, not bcrypt/argon2 - this
is a portfolio/demo tool, not a system holding real client financial
data in production, and pulling in a compiled dependency (bcrypt often
needs a wheel build, exactly the kind of friction this project hit and
documented when picking Python versions for pydantic-core) isn't worth
it for the security margin it would add here. If this app ever handles
real audit data, swap this for argon2 first.
"""

import hashlib
import hmac
import secrets

_ITERATIONS = 260_000
_ALGORITHM = "sha256"


def hash_password(password: str) -> tuple[str, str]:
    """Returns (password_hash, salt), both hex strings, ready to store."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(_ALGORITHM, password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    digest = hashlib.pbkdf2_hmac(_ALGORITHM, password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS)
    return hmac.compare_digest(digest.hex(), password_hash)
