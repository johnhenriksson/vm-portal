import asyncio
import datetime
import hashlib
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from tests.test_web import csrf, login

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def esxi(tmp_path):
    """A fake ESXi WebMKS endpoint: TLS with a self-signed cert, speaks 'RFB 003.008' then echoes."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "esxi.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                       serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    seen = {}

    async def handler(conn):
        seen["path"] = conn.request.path
        await conn.send(b"RFB 003.008\n")
        async for msg in conn:
            await conn.send(b"echo:" + msg)

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state = {}

    async def main():
        server = await serve(handler, "127.0.0.1", 0, ssl=ctx, subprotocols=["binary"])
        state["port"] = server.sockets[0].getsockname()[1]
        state["stop"] = asyncio.Event()
        ready.set()
        await state["stop"].wait()
        server.close()

    th = threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True)
    th.start()
    ready.wait(5)
    der = cert.public_bytes(serialization.Encoding.DER)
    thumb = ":".join(f"{b:02X}" for b in hashlib.sha1(der).digest())
    yield {"port": state["port"], "thumbprint": thumb, "seen": seen}
    loop.call_soon_threadsafe(state["stop"].set)
    th.join(5)


@pytest.fixture
def setup(monkeypatch, esxi):
    from app import db, users, vsphere
    monkeypatch.setattr("app.main.login_failed_delay", lambda: asyncio.sleep(0))
    monkeypatch.setattr("app.console.vcenter_configured", lambda: True)
    ticket = vsphere.ConsoleTicket(host="127.0.0.1", port=esxi["port"], path="/ticket/secret-ticket",
                                   thumbprint=esxi["thumbprint"])
    monkeypatch.setattr(vsphere, "console_ticket", lambda name, moid=None: ticket)
    from app.main import app
    with TestClient(app) as c:
        users.create("alice", "purple-otter-41", "user")
        alice = users.by_name("alice")
        db.execute("INSERT INTO vms (name,status,template,cpus,memory_mb,disk_gb,ip_mode,created_at,owner_id)"
                   " VALUES ('alice-vm','ready','t',1,1024,10,'dhcp',?,?)", (db.now(), alice["id"]))
        yield c, ticket


def test_console_relays_through_pinned_tls(setup, esxi):
    c, _ = setup
    login(c, "alice", "purple-otter-41")
    assert c.get("/vms/alice-vm/console").status_code == 200
    with c.websocket_connect("/vms/alice-vm/console/ws", subprotocols=["binary"], headers=dict(ORIGIN)) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"
        ws.send_bytes(b"RFB 003.008\n")
        assert ws.receive_bytes() == b"echo:RFB 003.008\n"
        ws.close(1000)
        _wait_until_closed()
    assert esxi["seen"]["path"] == "/ticket/secret-ticket"


def _wait_until_closed(timeout=5.0):
    """Let the relay finish its cleanup before the test client cancels the app."""
    import time
    from app import console
    deadline = time.monotonic() + timeout
    while any(console._open.values()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not any(console._open.values())


@pytest.mark.parametrize("case", ["anonymous", "other-user", "bad-origin", "no-origin", "no-subprotocol"])
def test_console_rejections(setup, case):
    from app import users
    c, _ = setup
    if case == "other-user":
        users.create("bob", "green-walrus-72", "user")
        login(c, "bob", "green-walrus-72")
    elif case != "anonymous":
        login(c, "alice", "purple-otter-41")
    headers = {"bad-origin": {"origin": "http://evil.example"}, "no-origin": {}}.get(case, dict(ORIGIN))
    protocols = [] if case == "no-subprotocol" else ["binary"]
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/vms/alice-vm/console/ws", subprotocols=protocols, headers=headers) as ws:
            ws.receive_bytes()
    assert e.value.code in (1002, 1008)


def test_console_refuses_wrong_thumbprint(setup):
    c, ticket = setup
    ticket.thumbprint = "AA:" * 19 + "AA"
    login(c, "alice", "purple-otter-41")
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/vms/alice-vm/console/ws", subprotocols=["binary"], headers=dict(ORIGIN)) as ws:
            ws.receive_bytes()
    assert e.value.code == 1011 and "thumbprint" in e.value.reason


def test_console_button_only_for_running_vms(setup, monkeypatch):
    from app import vsphere
    c, _ = setup
    login(c, "alice", "purple-otter-41")
    on = vsphere.VMInfo("alice-vm", False, "poweredOn", "10.0.0.5")
    monkeypatch.setattr("app.main.vcenter_configured", lambda s=None: True)
    monkeypatch.setattr(vsphere, "vms_by_name", lambda fresh=False: {"alice-vm": on})
    assert "/vms/alice-vm/console" in c.get("/vms/alice-vm").text
    on.power_state = "poweredOff"
    assert "/vms/alice-vm/console" not in c.get("/vms/alice-vm").text
