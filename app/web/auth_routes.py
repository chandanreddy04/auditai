"""
Signup/login/logout plus the get_current_user dependency every other
route in this app now requires. Session-based auth via a signed cookie
(Starlette's SessionMiddleware, wired up in main.py) - the cookie holds
only a user id, never a password or anything sensitive, and it's
tamper-evident (signed with SECRET_KEY) even though it isn't encrypted.

No email verification, no password reset flow, no rate limiting on
login attempts - this is a small internal tool for a handful of
auditors at one firm, not a public-facing product. Real deployment
would need those before handling real client data; see README's Known
Limitations.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.models import User
from app.services import auth_service

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class NotAuthenticatedError(Exception):
    """Raised by get_current_user() when there's no valid session -
    caught by a handler in main.py that redirects to /login, so every
    protected route can just declare the dependency and not think
    about the unauthenticated case itself."""


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if user_id is None:
        raise NotAuthenticatedError()
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        raise NotAuthenticatedError()
    return user


@router.get("/signup")
def signup_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("signup.html", {"request": request, "error": None})


@router.post("/signup")
def signup(
    request: Request, name: str = Form(...), email: str = Form(...),
    password: str = Form(...), db: Session = Depends(get_db),
):
    email_norm = email.strip().lower()
    if len(password) < 8:
        return templates.TemplateResponse("signup.html", {"request": request, "error": "Password must be at least 8 characters."})

    existing = db.query(User).filter(User.email == email_norm).first()
    if existing is not None:
        return templates.TemplateResponse("signup.html", {"request": request, "error": "An account with that email already exists."})

    password_hash, salt = auth_service.hash_password(password)
    user = User(name=name.strip(), email=email_norm, password_hash=password_hash, password_salt=salt)
    db.add(user)
    db.commit()

    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=303)


@router.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if user is None or not user.is_active or not auth_service.verify_password(password, user.password_hash, user.password_salt):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Incorrect email or password."})

    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
