"""One-time messages after an action: the action stores one in the session, redirects, and
the page it lands on shows it once (post/redirect/get, so a reload doesn't repeat the
action or the message)."""
from fastapi import Request
from fastapi.responses import RedirectResponse

KEY = "flash"
MAX_LEN = 500       # the session is a cookie: keep it small


def flash(request: Request, notice: str | None = None, error: str | None = None) -> None:
    msg = {k: v[:MAX_LEN] for k, v in (("notice", notice), ("error", error)) if v}
    if msg:
        request.session[KEY] = msg


def redirect(request: Request, url: str, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    flash(request, notice, error)
    return RedirectResponse(url, status_code=303)


def take(request: Request) -> dict:
    """The waiting message, if any, removed so it shows only once. Polled partials (htmx)
    leave it for the full page."""
    if request.headers.get("HX-Request") or "session" not in request.scope:
        return {}
    msg = request.session.pop(KEY, None)
    return msg if isinstance(msg, dict) else {}
