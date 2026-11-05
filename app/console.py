"""Browser console for VMs: noVNC in the browser, relayed through the portal to ESXi.

    browser (noVNC) ──wss──► portal /vms/<name>/console/ws ──wss──► ESXi /ticket/<one-time ticket>

The portal checks the session, VM ownership and the request Origin, then asks
vCenter for a short-lived WebMKS ticket and relays bytes both ways. The browser
never sees the ticket or needs to reach (or trust) the ESXi hosts. The ESXi
certificate is pinned to the thumbprint vCenter reports for the ticket.
"""
import asyncio
import hashlib
import hmac
import logging
import ssl
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from . import db, vsphere
from .auth import client_ip, require_user, session_user
from .config import vcenter_configured

log = logging.getLogger(__name__)
router = APIRouter()

MAX_PER_USER = 3
MAX_SECONDS = 8 * 3600       # hard limit per console session
RECHECK_SECONDS = 30         # how often the session/user/ownership is re-validated
_open: dict[int, int] = {}   # user id -> open consoles


def render(*args, **kwargs):
    from .main import render as _render
    return _render(*args, **kwargs)


def can_access(vm: dict | None, user: dict | None) -> bool:
    return bool(vm and user and (user["role"] == "admin" or vm["owner_id"] == user["id"]))


@router.get("/vms/{name}/console")
async def console_page(request: Request, name: str, user: dict = Depends(require_user)):
    vm = db.active_vm(name)
    if not can_access(vm, user):
        raise HTTPException(404, f"No VM named {name!r}")
    return render(request, "console.html", vm=vm)


# --- ESXi side ----------------------------------------------------------------

def _thumbprint_matches(der: bytes, expected: str) -> bool:
    want = expected.replace(":", "").lower()
    algo = {40: hashlib.sha1, 64: hashlib.sha256}.get(len(want))
    return bool(algo) and hmac.compare_digest(algo(der).hexdigest(), want)


def pinned_context(host: str, port: int, thumbprint: str) -> ssl.SSLContext:
    """A TLS context that trusts exactly the ESXi certificate vCenter vouched for."""
    if not thumbprint:
        raise RuntimeError("vCenter did not report the ESXi host's certificate thumbprint")
    pem = ssl.get_server_certificate((host, port), timeout=10)
    if not _thumbprint_matches(ssl.PEM_cert_to_DER_cert(pem), thumbprint):
        raise RuntimeError(f"The certificate of {host} doesn't match the thumbprint vCenter reported")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False  # we connect by IP; trust comes from the pinned certificate
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # accept the pinned leaf as the trust anchor
    ctx.load_verify_locations(cadata=pem)
    return ctx


# --- browser side -------------------------------------------------------------

def _same_origin(ws: WebSocket) -> bool:
    """Blocks cross-site WebSocket hijacking: the Origin must be this site."""
    origin, host = ws.headers.get("origin"), ws.headers.get("host")
    return bool(origin and host) and urlsplit(origin).netloc == host


@router.websocket("/vms/{name}/console/ws")
async def console_ws(ws: WebSocket, name: str):
    user = session_user(ws.session)
    vm = db.active_vm(name)
    if not _same_origin(ws) or not can_access(vm, user):
        await ws.close(code=1008)  # policy violation; same answer for every reason
        return
    if not vcenter_configured() or vm["status"] != "ready":
        await ws.close(code=1011, reason="The VM is not ready")
        return
    if _open.get(user["id"], 0) >= MAX_PER_USER:
        await ws.close(code=1013, reason=f"At most {MAX_PER_USER} consoles at a time")
        return
    if "binary" not in ws.scope.get("subprotocols", []):
        await ws.close(code=1002)
        return

    _open[user["id"]] = _open.get(user["id"], 0) + 1
    started = time.monotonic()
    try:
        try:
            t = await asyncio.to_thread(vsphere.console_ticket, name)
            ctx = await asyncio.to_thread(pinned_context, t.host, t.port, t.thumbprint)
            esx = await connect(f"wss://{t.host}:{t.port}{t.path}", ssl=ctx, subprotocols=["binary"],
                                max_size=None, open_timeout=15, close_timeout=2, ping_interval=30)
        except Exception as e:  # noqa: BLE001
            log.warning("Console for %s failed: %s", name, e)
            await ws.close(code=1011, reason=_reason(e))
            return
        await ws.accept(subprotocol="binary")
        db.audit("vm.console", name, username=user["username"], ip=client_ip(ws))
        await _relay(ws, esx, user, name)
    finally:
        _open[user["id"]] -= 1
        log.info("Console for %s by %s closed after %ds", name, user["username"], time.monotonic() - started)


def _reason(e: Exception) -> str:
    text = getattr(e, "msg", None) or str(e) or type(e).__name__
    return text[:120]  # close reasons are limited to 123 bytes


async def _relay(ws: WebSocket, esx, user: dict, name: str) -> None:
    async def browser_to_esx():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes") if msg.get("bytes") is not None else msg.get("text")
            if data is not None:
                await esx.send(data)

    async def esx_to_browser():
        async for data in esx:
            if isinstance(data, bytes):
                await ws.send_bytes(data)
            else:
                await ws.send_text(data)

    async def guard():
        """End the console when the session is revoked, access is lost, or time is up."""
        deadline = time.monotonic() + MAX_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(RECHECK_SECONDS)
            u = session_user(ws.session)
            if u is None or not can_access(db.active_vm(name), u):
                return

    tasks = [asyncio.create_task(c()) for c in (browser_to_esx, esx_to_browser, guard)]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(esx.close(), timeout=3)
        except (TimeoutError, ConnectionClosed, OSError):
            pass
        try:
            await ws.close()
        except (RuntimeError, ConnectionClosed):
            pass
