"""Browser console for VMs: noVNC in the browser, relayed through the portal.

    browser (noVNC) ──wss──► portal /vms/<name>/console/ws ──wss──► ESXi /ticket/<one-time ticket>
                                                         └─wss──► Proxmox …/vncwebsocket

The portal checks the session, VM ownership and the request Origin, then asks the VM's
backend for a short-lived console ticket and relays bytes both ways. The browser never
sees the ticket or needs to reach (or trust) the hosts. On vSphere the ESXi certificate
is pinned to the thumbprint vCenter reports for the ticket. On Proxmox the node's
certificate is the one the backend pins (or one the system trusts), and the portal
answers the VNC password challenge itself: the browser gets a console that needs none.
"""
import asyncio
import hashlib
import hmac
import logging
import ssl
import struct
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from . import backends, db, provision
from .auth import client_ip, require_user, session_user

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
    """A TLS context that trusts exactly the certificate the platform vouched for."""
    if not thumbprint:
        raise RuntimeError("vCenter did not report the ESXi host's certificate thumbprint")
    pem = ssl.get_server_certificate((host, port), timeout=10)
    if not _thumbprint_matches(ssl.PEM_cert_to_DER_cert(pem), thumbprint):
        raise RuntimeError(f"The certificate of {host} doesn't match the thumbprint the platform reported")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False  # we connect by IP; trust comes from the pinned certificate
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # accept the pinned leaf as the trust anchor
    ctx.load_verify_locations(cadata=pem)
    return ctx


def tls_for(t) -> ssl.SSLContext:
    if t.thumbprint:
        return pinned_context(t.host, t.port, t.thumbprint)
    if t.ca_verified:
        return ssl.create_default_context()
    return pinned_context(t.host, t.port, "")   # refuses: no way to know the certificate


# --- VNC authentication (Proxmox) -----------------------------------------------

class Stream:
    """Exact-length reads over a WebSocket's messages (RFB doesn't respect message borders)."""

    def __init__(self, recv):
        self._recv, self.buf = recv, b""

    async def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            data = await self._recv()
            if data is None:
                raise ConnectionError("The console connection closed during the handshake")
            self.buf += data if isinstance(data, bytes) else data.encode("latin-1")
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def vnc_response(password: str, challenge: bytes) -> bytes:
    """VNC authentication: the challenge encrypted with DES, keyed by the password with each
    byte's bits reversed (as the RFB protocol defines it). 3DES with one key repeated is DES."""
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in password.encode("latin-1", "replace")[:8].ljust(8, b"\0"))
    enc = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    return enc.update(challenge) + enc.finalize()


async def _security_result(server: Stream) -> None:
    (status,) = struct.unpack(">I", await server.read(4))
    if status != 0:
        (n,) = struct.unpack(">I", await server.read(4))
        reason = (await server.read(min(n, 1024))).decode("utf-8", "replace")
        raise RuntimeError(f"The console refused the login: {reason or 'no reason given'}")


async def vnc_login(server: Stream, send, password: str) -> None:
    """Log in to an RFB 3.8 server with VNC authentication."""
    version = await server.read(12)
    if not version.startswith(b"RFB 003."):
        raise RuntimeError("The console didn't answer like a VNC server")
    await send(b"RFB 003.008\n")
    (count,) = struct.unpack(">B", await server.read(1))
    if count == 0:
        (n,) = struct.unpack(">I", await server.read(4))
        raise RuntimeError("The console refused: " + (await server.read(min(n, 1024))).decode("utf-8", "replace"))
    types = await server.read(count)
    if 2 in types:
        await send(b"\x02")
        await send(vnc_response(password, await server.read(16)))
    elif 1 in types:
        await send(b"\x01")
    else:
        raise RuntimeError("The console offers no login the portal can do")
    await _security_result(server)


async def browser_login(browser: Stream, send) -> None:
    """Toward the browser: an RFB 3.8 server that needs no password (the portal has
    already logged in on its behalf)."""
    await send(b"RFB 003.008\n")
    version = await browser.read(12)
    if not version.startswith(b"RFB 003."):
        raise RuntimeError("The browser didn't speak VNC")
    await send(b"\x01\x01")                       # one security type: none
    if (await browser.read(1)) != b"\x01":
        raise RuntimeError("The browser chose a security type that wasn't offered")
    await send(struct.pack(">I", 0))                # SecurityResult: OK


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
    b = backends.of_vm(vm)
    if b is None or not b.configured or vm["status"] != "ready":
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
        upstream_buf = b""
        try:
            t = await asyncio.to_thread(b.platform.console_ticket, b, name, provision.vm_moid(name))
            ctx = await asyncio.to_thread(tls_for, t)
            esx = await connect(f"wss://{t.host}:{t.port}{t.path}", ssl=ctx, subprotocols=["binary"],
                                additional_headers=t.headers or None, max_size=None, open_timeout=15,
                                close_timeout=2, ping_interval=30)
            if t.vnc_password is not None:
                upstream = Stream(esx.recv)
                await asyncio.wait_for(vnc_login(upstream, esx.send, t.vnc_password), timeout=20)
                upstream_buf = upstream.buf
        except Exception as e:  # noqa: BLE001
            log.warning("Console for %s failed: %s", name, e)
            await ws.close(code=1011, reason=_reason(e))
            return
        await ws.accept(subprotocol="binary")
        browser_buf = b""
        if t.vnc_password is not None:
            async def browser_recv():
                msg = await ws.receive()
                return None if msg["type"] == "websocket.disconnect" else (msg.get("bytes") or
                                                                           (msg.get("text") or "").encode())
            browser = Stream(browser_recv)
            try:
                await asyncio.wait_for(browser_login(browser, ws.send_bytes), timeout=20)
            except Exception as e:  # noqa: BLE001
                log.warning("Console for %s: browser handshake failed: %s", name, e)
                await esx.close()
                await ws.close(code=1002)
                return
            browser_buf = browser.buf
        db.audit("vm.console", name, username=user["username"], ip=client_ip(ws))
        if browser_buf:
            await esx.send(browser_buf)
        if upstream_buf:
            await ws.send_bytes(upstream_buf)
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
