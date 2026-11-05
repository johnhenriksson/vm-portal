"""Sessions, roles and CSRF protection.

The session cookie (signed, not encrypted) holds only the user id, the user's
session_version and a CSRF token. The user is re-read on every request, so
disabling a user or changing their password ends their other sessions.
"""
import hmac
import secrets

from fastapi import Depends, HTTPException, Request
from markupsafe import Markup
from starlette.requests import HTTPConnection

from . import users

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class LoginRequired(Exception):
    pass


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def session_user(session: dict) -> dict | None:
    """The session's user if it still exists, is enabled and the session wasn't revoked."""
    uid, sv = session.get("uid"), session.get("sv")
    u = users.get(uid) if isinstance(uid, int) else None
    if u and (u["disabled"] or u["session_version"] != sv):
        u = None
    return u


def current_user(request: HTTPConnection) -> dict | None:
    if hasattr(request.state, "user"):
        return request.state.user
    u = session_user(request.session)
    if u is None and request.session.get("uid") is not None:
        request.session.pop("uid", None)
        request.session.pop("sv", None)
    request.state.user = u
    return u


def require_user(request: Request) -> dict:
    u = current_user(request)
    if u is None:
        raise LoginRequired()
    return u


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Only admins can do that")
    return user


def start_session(request: Request, user: dict) -> None:
    request.session.clear()
    request.session.update({"uid": user["id"], "sv": user["session_version"], "csrf": secrets.token_urlsafe(32)})
    request.state.user = user


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(32)
    return tok


def csrf_field(request: Request) -> Markup:
    return Markup('<input type="hidden" name="csrf_token" value="{}">').format(csrf_token(request))


async def csrf_protect(request: HTTPConnection) -> None:
    """App-wide dependency: every state-changing request must carry the session's CSRF token.

    WebSockets are checked by Origin in their handlers instead (browsers send no CSRF token there).
    """
    if request.scope["type"] != "http" or request.method in SAFE_METHODS:
        return
    sent = request.headers.get("X-CSRF-Token")
    if not sent:
        form = await request.form()  # cached by Starlette; route Form() params still work
        sent = form.get("csrf_token")
    expected = request.session.get("csrf")
    if not (isinstance(sent, str) and expected and hmac.compare_digest(sent, expected)):
        raise HTTPException(403, "Your session expired or the form is stale. Reload the page and try again.")
