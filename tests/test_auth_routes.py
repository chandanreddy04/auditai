"""
Uses its own TestClient (not the shared, already-logged-in one in
test_api.py) so it can freely test the logged-out state.
"""

from fastapi.testclient import TestClient

from app.database.session import init_db
from app.main import app

init_db()


def test_protected_route_redirects_to_login_when_logged_out():
    client = TestClient(app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_signup_then_access_protected_route():
    client = TestClient(app)
    resp = client.post(
        "/signup",
        data={"name": "Alice Auditor", "email": "alice@example.com", "password": "hunter22222"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    resp = client.get("/")
    assert resp.status_code == 200


def test_signup_rejects_duplicate_email():
    client = TestClient(app)
    client.post("/signup", data={"name": "Bob", "email": "bob@example.com", "password": "password123"})
    resp = client.post("/signup", data={"name": "Bob Two", "email": "bob@example.com", "password": "password123"})
    assert resp.status_code == 200  # re-renders the signup form with an error, not a redirect
    assert "already exists" in resp.text


def test_signup_rejects_short_password():
    client = TestClient(app)
    resp = client.post("/signup", data={"name": "Carl", "email": "carl@example.com", "password": "short"})
    assert "at least 8 characters" in resp.text


def test_login_with_correct_password_succeeds():
    client = TestClient(app)
    client.post("/signup", data={"name": "Dana", "email": "dana@example.com", "password": "correctpassword"})
    client.post("/logout")

    resp = client.post("/login", data={"email": "dana@example.com", "password": "correctpassword"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    resp = client.get("/")
    assert resp.status_code == 200


def test_login_with_wrong_password_fails():
    client = TestClient(app)
    client.post("/signup", data={"name": "Eve", "email": "eve@example.com", "password": "correctpassword"})
    client.post("/logout")

    resp = client.post("/login", data={"email": "eve@example.com", "password": "wrongpassword"})
    assert resp.status_code == 200
    assert "Incorrect email or password" in resp.text

    # Confirm the failed login did NOT grant access.
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303


def test_login_with_unknown_email_fails():
    client = TestClient(app)
    resp = client.post("/login", data={"email": "nobody@example.com", "password": "whatever123"})
    assert "Incorrect email or password" in resp.text


def test_logout_revokes_access():
    client = TestClient(app)
    client.post("/signup", data={"name": "Frank", "email": "frank@example.com", "password": "somepassword"})
    assert client.get("/").status_code == 200

    client.post("/logout")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_signed_in_user_redirected_away_from_login_and_signup_pages():
    client = TestClient(app)
    client.post("/signup", data={"name": "Grace", "email": "grace@example.com", "password": "somepassword"})

    assert client.get("/login", follow_redirects=False).status_code == 303
    assert client.get("/signup", follow_redirects=False).status_code == 303
