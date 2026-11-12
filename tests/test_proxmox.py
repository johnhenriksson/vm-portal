"""Proxmox VE backends: the module is run against a small fake of Proxmox's HTTPS API (a
real TLS server with a self-signed certificate, so the pinning is really exercised)."""
import asyncio
import datetime
import json
import re
import ssl
import struct
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from app import backends, config, console, db, provision, users
from app.platforms import proxmox

TOKEN_ID, SECRET = "portal@pve!vmportal", "0f0e0d0c-aaaa-bbbb-cccc-1234567890ab"
TEMPLATE = "ubuntu-2604-docker-20261101-1200"
UPID = "UPID:pve1:00001234:00005678:6543210F:task:100:portal@pve!vmportal:"


def api():
    return {
        "GET /version": {"version": "9.1.2", "release": "9.1"},
        "GET /nodes": [{"node": "pve2", "status": "offline"}, {"node": "pve1", "status": "online"}],
        "GET /nodes/pve1/storage": [
            {"storage": "local", "type": "dir", "content": "iso,vztmpl,backup", "avail": 50 * 1024 ** 3,
             "total": 100 * 1024 ** 3, "active": 1, "enabled": 1},
            {"storage": "local-lvm", "type": "lvmthin", "content": "images,rootdir", "avail": 200 * 1024 ** 3,
             "total": 400 * 1024 ** 3, "active": 1, "enabled": 1},
            {"storage": "off", "type": "nfs", "content": "images,iso", "active": 0, "enabled": 1}],
        "GET /nodes/pve1/network?type=any_bridge": [{"iface": "vmbr1"}, {"iface": "vmbr0"}],
        "GET /pools": [{"poolid": "portal"}],
        "GET /cluster/resources?type=vm": [
            {"type": "qemu", "vmid": 100, "node": "pve1", "name": TEMPLATE, "status": "stopped", "template": 1},
            {"type": "qemu", "vmid": 101, "node": "pve1", "name": "web-01", "status": "running", "template": 0},
            {"type": "qemu", "vmid": 102, "node": "pve1", "name": "db-01", "status": "stopped", "template": 0},
            {"type": "lxc", "vmid": 200, "node": "pve1", "name": "a-container", "status": "running"}],
        "GET /nodes/pve1/qemu/100/config": {"description": "Built by vm-portal", "scsi0": "local-lvm:base-100-disk-0,size=25G",
                                            "meta": "creation-qemu=9.0.2,ctime=1793534400"},
        "GET /nodes/pve1/qemu/101/config": {"description": "Created by vm-portal from x", "agent": "1,fstrim_cloned_disks=1",
                                            "meta": "creation-qemu=9.0.2,ctime=1793620800", "onboot": 0,
                                            "ide2": "local:iso/vm-portal-seed-web-01.iso,media=cdrom"},
        "GET /nodes/pve1/qemu/102/config": {"agent": "1", "ipconfig0": "ip=10.0.0.12/24,gw=10.0.0.1"},
        "GET /nodes/pve1/qemu/101/agent/network-get-interfaces": {"result": [
            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}]},
            {"name": "ens18", "ip-addresses": [{"ip-address": "10.0.0.11", "ip-address-type": "ipv4"},
                                               {"ip-address": "fe80::1", "ip-address-type": "ipv6"}]},
            {"name": "docker0", "ip-addresses": [{"ip-address": "172.17.0.1", "ip-address-type": "ipv4"}]}]},
        "GET /nodes/pve1/storage/local/content?content=iso": [
            {"volid": "local:iso/ubuntu-26.04.iso"}, {"volid": "local:iso/debian-13.iso"},
            {"volid": "local:iso/vm-portal-seed-web-01-clean.iso"}],
        f"GET /nodes/pve1/tasks/{UPID}/status": {"status": "stopped", "exitstatus": "OK"},
        "POST /nodes/pve1/qemu/101/status/stop": UPID,
        "POST /nodes/pve1/qemu/101/status/shutdown": UPID,
        "PUT /nodes/pve1/qemu/101/config": None,
        "DELETE /nodes/pve1/qemu/100?purge=1": UPID,
        "POST /nodes/pve1/qemu/101/agent/ping": {},
        "POST /nodes/pve1/qemu/101/vncproxy": {"port": "5900", "ticket": "PVEVNC:abc+/=", "user": TOKEN_ID},
        "POST /nodes/pve1/storage/local/upload": UPID,
    }


class FakePve:
    def __init__(self, tmp_path):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pve1.example")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
        (tmp_path / "pve.pem").write_bytes(
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()) + cert.public_bytes(serialization.Encoding.PEM))
        self.fingerprint = proxmox.fingerprint_of(cert.public_bytes(serialization.Encoding.DER))
        self.api = api()
        self.seen: list[tuple[str, str, bytes]] = []      # (method, path, body)
        self.auth: list[str | None] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.removeprefix("/api2/json")
                fake.seen.append((self.command, unquote(path), body))
                fake.auth.append(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != f"PVEAPIToken={TOKEN_ID}={SECRET}":
                    return self.reply(401, None, "authentication failure")
                key = f"{self.command} {unquote(path)}"
                if self.command == "DELETE" and "/content/" in path:
                    return self.reply(200, UPID)
                if key not in fake.api:
                    return self.reply(500, None, "QEMU guest agent is not running")
                self.reply(200, fake.api[key])

            do_GET = do_POST = do_PUT = do_DELETE = handle_any

            def reply(self, status, data, reason=None):
                body = json.dumps({"data": data}).encode()
                self.send_response(status, reason)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tmp_path / "pve.pem")
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.address = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def conn(self, **kw):
        return proxmox.Conn(**{"server": self.address, "token_id": TOKEN_ID, "token_secret": SECRET,
                               "fingerprint": self.fingerprint, **kw})

    def calls(self, method=None):
        return [(m, p) for m, p, _ in self.seen if method is None or m == method]


@pytest.fixture
def pve(tmp_path):
    fake = FakePve(tmp_path)
    proxmox.close()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()
    proxmox.close()


@pytest.fixture
def b(pve):
    """A Proxmox VE backend beside the environment's vCenter, set up like the UI would."""
    bid = backends.create("Proxmox Oslo", "proxmox", {
        "proxmox_server": pve.address, "proxmox_token_id": TOKEN_ID, "proxmox_token_secret": SECRET,
        "proxmox_fingerprint": pve.fingerprint.replace(":", "").lower(), "proxmox_node": "pve1",
        "proxmox_storage": "local-lvm", "proxmox_iso_storage": "local", "proxmox_bridge": "vmbr0",
        "ip_pool_start": "10.9.0.10", "ip_pool_end": "10.9.0.12", "ip_gateway": "10.9.0.1"}, by="test")
    return backends.get(bid)


def test_a_backend_beside_the_vcenter(b):
    assert [x.type for x in backends.all_backends()] == ["vsphere", "proxmox"]
    assert b.configured and b.platform is proxmox and b.cfg.proxmox_fingerprint == b.cfg.proxmox_fingerprint.upper()
    assert b.cfg.proxmox_fingerprint.count(":") == 31                 # stored the way Proxmox shows it
    row = db.one("SELECT config, secrets FROM backends WHERE id=?", (b.id,))
    assert SECRET not in row["config"] + row["secrets"]                # the token secret is encrypted


def test_lists_vms_templates_isos_and_addresses(b):
    vms = proxmox.vms_by_name(b)
    assert set(vms) == {"web-01", "db-01"}                             # no templates, no containers
    web, dbvm = vms["web-01"], vms["db-01"]
    assert web.power_state == "poweredOn" and web.tools_running and web.ips == ["10.0.0.11"] and web.vm_id == "101"
    assert web.notes.startswith("Created by vm-portal") and web.created.year == 2026
    assert dbvm.power_state == "poweredOff" and dbvm.ip == "10.0.0.12" and not dbvm.tools_running
    assert proxmox.used_ips(b) == {"10.0.0.11", "10.0.0.12"}           # not lo, not docker0
    assert [(t.name, t.disk_gb) for t in proxmox.templates(b)] == [(TEMPLATE, 25)]
    assert web.disk_gb is None                                          # only templates say
    assert proxmox.name_exists(b, "web-01") and not proxmox.name_exists(b, "nope")
    assert proxmox.list_isos(b) == ["local:iso/debian-13.iso", "local:iso/ubuntu-26.04.iso"]   # not seeds
    assert proxmox.api_host(b) == "127.0.0.1"


def test_details_are_cached_but_the_list_is_fresh(b, pve):
    proxmox.all_vms(b)
    n = len(pve.seen)
    proxmox.all_vms(b, fresh=True)
    assert pve.calls()[n:] == [("GET", "/cluster/resources?type=vm")]


def test_wrong_fingerprint_sends_nothing(pve):
    other = ":".join(["AB"] * 32)
    with pytest.raises(RuntimeError, match="doesn't match the saved fingerprint"):
        proxmox.request(pve.conn(fingerprint=other), "GET", ("version",))
    assert pve.seen == []                                               # in particular not the token


def test_untrusted_certificate_is_refused_and_shows_its_fingerprint(pve):
    with pytest.raises(RuntimeError) as e:
        proxmox.request(pve.conn(fingerprint=""), "GET", ("version",))
    assert "isn't trusted" in str(e.value) and pve.fingerprint in str(e.value)
    assert pve.seen == []


def test_bad_token_and_errors_never_echo_the_secret(pve):
    with pytest.raises(proxmox.ApiError, match="refused the API token") as e:
        proxmox.request(pve.conn(token_secret="wrong-secret"), "GET", ("version",))
    assert "wrong-secret" not in str(e.value)
    with pytest.raises(proxmox.ApiError, match="answered 500"):
        proxmox.request(pve.conn(), "GET", ("nodes", "pve1", "qemu", 999, "config"))


def test_path_parts_cannot_leave_their_place():
    assert proxmox._path("nodes", "a/../../access", "qemu", 1) == "/api2/json/nodes/a%2F..%2F..%2Faccess/qemu/1"


def test_inventory(pve):
    inv = proxmox.inventory(**{k: getattr(pve.conn(), k) for k in ("server", "token_id", "token_secret", "fingerprint")})
    assert inv["about"] == "Proxmox VE 9.1.2" and inv["pools"] == ["portal"]
    pve1 = next(n for n in inv["nodes"] if n["name"] == "pve1")
    assert [s["name"] for s in pve1["storages"]] == ["local", "local-lvm"]   # not the inactive one
    assert pve1["bridges"] == ["vmbr0", "vmbr1"]
    assert not next(n for n in inv["nodes"] if n["name"] == "pve2")["online"]


def test_parsing():
    assert proxmox.created_at("creation-qemu=9.0.2,ctime=1712345678").year == 2024
    assert proxmox.created_at("") is None and proxmox.created_at("ctime=abc") is None
    assert proxmox.VM_ID_RE.match("104") and not proxmox.VM_ID_RE.match("vm-42") and not proxmox.VM_ID_RE.match("99")
    assert proxmox._disk_gb({"scsi0": "local-lvm:base-100-disk-0,size=25G"}) == 25
    assert proxmox._disk_gb({"virtio0": "x,size=2T"}) == 2048 and proxmox._disk_gb({}) == 0
    from datetime import datetime, timedelta, timezone
    booted = proxmox.booted_at({"status": "running", "uptime": 600})
    assert abs(datetime.now(timezone.utc) - timedelta(seconds=600) - booted) < timedelta(seconds=2)
    assert proxmox.booted_at({"status": "stopped", "uptime": 0}) is None and proxmox.booted_at({"status": "running"}) is None
    assert not proxmox.GUEST_POWER_NEEDS_TOOLS          # ACPI works without the guest agent


@pytest.mark.parametrize("field, bad", [
    ("proxmox_token_id", "root@pam"), ("proxmox_token_id", "a@b!c d"), ("proxmox_node", "pve1/../x"),
    ("proxmox_storage", "local lvm"), ("proxmox_fingerprint", "AB:CD"), ("proxmox_token_secret", "a b"),
    ("proxmox_server", "https://pve"),
])
def test_settings_are_validated(field, bad):
    with pytest.raises(ValueError):
        backends.validate("proxmox", {field: bad})


# --- acting on VMs -----------------------------------------------------------------------

def test_power_acts_on_the_recorded_vm(b, pve):
    proxmox.power(b, "web-01", "off", "101")
    proxmox.power(b, "web-01", "shutdown", "101")
    assert ("POST", "/nodes/pve1/qemu/101/status/stop") in pve.calls()
    assert ("GET", f"/nodes/pve1/tasks/{UPID}/status") in pve.calls()     # waited for the task
    with pytest.raises(LookupError, match="no longer"):
        proxmox.power(b, "web-01", "off", "102")                         # 102 is another VM now
    with pytest.raises(ValueError):
        proxmox.power(b, "web-01", "explode", "101")


def test_autostart_is_the_vms_start_at_boot(b, pve):
    st = proxmox.autostart(b, "web-01", "101")
    assert not st.enabled and st.host == "pve1" and st.host_enabled
    proxmox.set_autostart(b, "web-01", "101", True)
    [(_, _, body)] = [x for x in pve.seen if x[0] == "PUT"]
    assert parse_qs(body.decode()) == {"onboot": ["1"]}


def test_delete_template_only_deletes_templates(b, pve):
    proxmox.delete_template(b, TEMPLATE)
    assert ("DELETE", "/nodes/pve1/qemu/100?purge=1") in pve.calls()
    with pytest.raises(LookupError):
        proxmox.delete_template(b, "web-01")                             # a VM, not a template


def test_console_ticket_stays_with_the_portal(b):
    t = proxmox.console_ticket(b, "web-01", "101")
    assert t.path.startswith("/api2/json/nodes/pve1/qemu/101/vncwebsocket?port=5900&vncticket=PVEVNC%3Aabc%2B%2F%3D")
    assert t.vnc_password == "PVEVNC:abc+/=" and t.headers["Authorization"].startswith("PVEAPIToken=")
    assert t.thumbprint == b.cfg.proxmox_fingerprint and not t.ca_verified
    with pytest.raises(RuntimeError, match="not powered on"):
        proxmox.console_ticket(b, "db-01", "102")


async def test_vnc_login_answers_the_challenge_and_hides_it_from_the_browser():
    challenge = bytes(range(16))

    class Pipe:
        def __init__(self, data):
            self.inbox, self.sent = list(data), []

        async def recv(self):
            return self.inbox.pop(0) if self.inbox else None

        async def send(self, data):
            self.sent.append(data)

    server = Pipe([b"RFB 003.008\n", b"\x02\x01\x02", challenge, struct.pack(">I", 0)])
    await console.vnc_login(console.Stream(server.recv), server.send, "PVEVNC:abc")
    assert server.sent == [b"RFB 003.008\n", b"\x02", console.vnc_response("PVEVNC:abc", challenge)]
    # VNC passwords are 8 bytes; what follows doesn't count (hence Proxmox's generated password).
    assert console.vnc_response("PVEVNC:a", challenge) != console.vnc_response("PVEVNC:b", challenge)
    assert console.vnc_response("PVEVNC:abc", challenge) == console.vnc_response("PVEVNC:abd", challenge)
    refused = Pipe([b"RFB 003.008\n", b"\x01\x02", challenge, struct.pack(">I", 1), struct.pack(">I", 6), b"failed"])
    with pytest.raises(RuntimeError, match="refused the login: failed"):
        await console.vnc_login(console.Stream(refused.recv), refused.send, "x")
    browser = Pipe([b"RFB 003.008\n", b"\x01"])
    await console.browser_login(console.Stream(browser.recv), browser.send)
    assert browser.sent == [b"RFB 003.008\n", b"\x01\x01", struct.pack(">I", 0)]   # no password asked


def test_vnc_des_matches_the_rfb_definition():
    """DES of the challenge, keyed by the password with each byte's bits mirrored."""
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    key = bytes(int(f"{c:08b}"[::-1], 2) for c in b"pass\0\0\0\0")
    enc = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    assert console.vnc_response("pass", b"0123456789abcdef") == enc.update(b"0123456789abcdef") + enc.finalize()
    assert int(f"{0x61:08b}"[::-1], 2) == 0x86


# --- the seed ISO --------------------------------------------------------------------------

def test_seed_files_put_the_network_in_its_own_file():
    md = "instance-id: web-01-1\nlocal-hostname: web-01\nredact: [userdata]\ncleanup-guestinfo: [userdata]\n" \
         "network:\n  version: 2\n  ethernets:\n    eth0:\n      addresses: [10.0.0.5/24]\n"
    files = proxmox.seed_files(md, "#cloud-config\nusers: []\n")
    assert set(files) == {"meta-data", "user-data", "network-config"}
    assert "redact" not in files["meta-data"] and "network" not in files["meta-data"]
    assert "10.0.0.5/24" in files["network-config"] and files["user-data"].startswith("#cloud-config")
    assert set(proxmox.seed_files("instance-id: x\n", "u")) == {"meta-data", "user-data"}   # DHCP
    assert "ssh" not in proxmox.clean_user_data() and "passwd" not in proxmox.clean_user_data()


def test_seed_iso_is_labelled_cidata(tmp_path):
    out = tmp_path / "seed.iso"
    proxmox.build_iso({"meta-data": "instance-id: x\n", "user-data": "#cloud-config\n"}, out)
    data = out.read_bytes()
    assert data[0x8000 + 40:0x8000 + 46] == b"CIDATA"                 # the primary volume descriptor's label


async def test_create_uploads_the_seed_and_clones_the_template_by_id(b, pve, data_dir):
    class Log:
        text = ""

        def step(self, t):
            self.text += t + "\n"

        def write(self, t):
            self.text += t
    req = provision.VMRequest(name="new-01", template=TEMPLATE, cpus=2, memory_mb=2048, disk_gb=10,
                              ssh_keys=["ssh-ed25519 AAAAkey3 x@y"], backend_id=b.id)
    d = data_dir / "vms" / "new-01"
    d.mkdir(parents=True)
    extra = await proxmox.before_create(b, req, d, Log())
    assert extra == {"template_vmid": 100, "template_node": "pve1", "seed_iso": "local:iso/vm-portal-seed-new-01.iso",
                     "bios": "seabios", "disk_gb": 25}                  # never below the template's disk
    upload = next(body for m, p, body in pve.seen if (m, p) == ("POST", "/nodes/pve1/storage/local/upload"))
    assert b'name="content"\r\n\r\niso' in upload and b'filename="vm-portal-seed-new-01.iso"' in upload
    assert b"CIDATA" in upload and b"AAAAkey3" in upload               # the real seed, with the keys
    assert not list(d.iterdir())                                        # nothing left behind locally
    assert set(proxmox.tf_placement(b)) | set(extra) | {"name"} >= {"node", "datastore", "bridge", "template_vmid"}


def test_clean_seed_swaps_in_a_copy_without_secrets(b, pve, data_dir):
    d = data_dir / "vms" / "web-01"
    d.mkdir(parents=True)
    (d / "terraform.tfvars.json").write_text(json.dumps({"metadata": "instance-id: web-01-1\n",
                                                         "userdata": "#cloud-config\npassword-hash: secret\n"}))
    db.execute("INSERT INTO vms (name, status, template, cpus, memory_mb, disk_gb, ip_mode, created_at, backend_id,"
               " seed_pending) VALUES ('web-01', 'ready', 't', 1, 1024, 10, 'dhcp', ?, ?, 1)", (db.now(), b.id))
    assert proxmox.clean_seed(b, "web-01", "101", wait=0)
    upload = next(body for m, p, body in pve.seen if (m, p) == ("POST", "/nodes/pve1/storage/local/upload"))
    assert b"vm-portal-seed-web-01-clean.iso" in upload and b"instance-id: web-01-1" in upload
    assert b"secret" not in upload
    put = next(body for m, p, body in pve.seen if m == "PUT")
    assert parse_qs(put.decode()) == {"ide2": ["local:iso/vm-portal-seed-web-01-clean.iso,media=cdrom"]}
    assert ("DELETE", "/nodes/pve1/storage/local/content/local:iso/vm-portal-seed-web-01.iso") in pve.calls()
    assert db.active_vm("web-01")["seed_pending"] == 0
    with pytest.raises(ValueError):
        proxmox.delete_iso(b, "local:iso/ubuntu-26.04.iso")             # never anything but a seed


def test_tofu_and_packer_get_the_pinned_certificate(b, pve, data_dir):
    env = proxmox.tf_env(b)
    assert env["TF_VAR_proxmox_endpoint"] == f"https://{pve.address}/"
    assert env["TF_VAR_proxmox_api_token"] == f"{TOKEN_ID}={SECRET}"
    pem = open(env["SSL_CERT_FILE"]).read()
    assert proxmox.fingerprint_of(ssl.PEM_cert_to_DER_cert(pem)) == pve.fingerprint
    assert proxmox.packer_vars(b)["SSL_CERT_FILE"] == env["SSL_CERT_FILE"]
    assert proxmox.packer_vars(b)["PKR_VAR_proxmox_url"].endswith("/api2/json")
    db.execute("UPDATE backends SET config=? WHERE id=?",
               (json.dumps({**json.loads(db.one("SELECT config FROM backends WHERE id=?", (b.id,))["config"]),
                            "proxmox_fingerprint": ":".join(["AB"] * 32)}), b.id))
    with pytest.raises(RuntimeError, match="doesn't match"):
        proxmox.tf_env(backends.get(b.id))


def test_import_checks_the_vm(b):
    assert proxmox.import_problems(b, "101", "web-01", {}) == []
    assert "named 'db-01'" in proxmox.import_problems(b, "102", "web-01", {})[0]
    assert "no VM with id 999" in proxmox.import_problems(b, "999", "web-01", {})[0]
    assert any("didn't create it" in p for p in proxmox.import_problems(b, "102", "db-01", {}))


# --- the pages --------------------------------------------------------------------------

def login(c):
    token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"username": "admin", "password": "test-password-123", "csrf_token": token})
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get("/account").text).group(1)


def client():
    from app.main import app
    return TestClient(app)


def test_pages_with_two_backends(b, monkeypatch):
    from app.platforms import vsphere
    monkeypatch.setattr(vsphere, "templates", lambda x: [])
    monkeypatch.setattr(vsphere, "vms_by_name", lambda x, fresh=False: {})
    with client() as c:
        token = login(c)
        page = c.get("/settings/backends").text
        assert "Proxmox Oslo" in page and "vCenter" in page and SECRET not in page
        page = c.get(f"/settings/backends/{b.id}").text
        assert SECRET not in page and "API token secret" in page
        r = c.post(f"/settings/backends/{b.id}/placement", data={"csrf_token": token})
        assert "Connected to Proxmox VE 9.1.2" in r.text and "local-lvm" in r.text and "vmbr0" in r.text
        new = c.get("/vms/new").text
        assert "Proxmox Oslo (Proxmox VE)" in new and 'name="backend_id"' in new
        new = c.get(f"/vms/new?backend={b.id}").text
        assert TEMPLATE in new and "gateway 10.9.0.1" in new and f"backend={b.id}" in new
        assert TEMPLATE in c.get("/images").text and "Proxmox Oslo" in c.get("/images").text


def test_users_only_get_the_backends_they_may_use(b, monkeypatch):
    from app.platforms import vsphere
    monkeypatch.setattr(vsphere, "templates", lambda x: [])
    backends.update(b, {}, "test", admin_only=True)
    users.create("alice", "correct horse battery", "user")
    with client() as c:
        token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
        c.post("/login", data={"username": "alice", "password": "correct horse battery", "csrf_token": token})
        assert "Proxmox Oslo" not in c.get("/vms/new").text
        assert c.get(f"/vms/new?backend={b.id}").status_code == 404
        token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/vms/new").text).group(1)
        r = c.post("/vms", data={"csrf_token": token, "name": "new-01", "template": TEMPLATE, "cpus": 2,
                                 "memory_mb": 2048, "disk_gb": 40, "backend_id": str(b.id)})
        assert r.status_code == 400 and "can&#39;t create VMs on that backend" in r.text
    assert db.one("SELECT COUNT(*) AS n FROM vms")["n"] == 0


def test_a_backend_with_vms_cannot_be_removed(b):
    db.execute("INSERT INTO vms (name, status, template, cpus, memory_mb, disk_gb, ip_mode, created_at, backend_id)"
               " VALUES ('web-01', 'ready', 't', 1, 1024, 10, 'dhcp', ?, ?)", (db.now(), b.id))
    with client() as c:
        token = login(c)
        r = c.post(f"/settings/backends/{b.id}/delete", data={"csrf_token": token, "confirm": b.name})
        assert r.status_code == 400 and "still has 1 VM" in r.text
        db.execute("UPDATE vms SET status='destroyed'")
        r = c.post(f"/settings/backends/{b.id}/delete", data={"csrf_token": token, "confirm": b.name},
                   follow_redirects=False)
        assert r.status_code == 303 and backends.get(b.id) is None
    assert db.one("SELECT 1 FROM audit WHERE action='backend.deleted'")


# --- VLAN tags ----------------------------------------------------------------------------

def test_vlan_tag_reaches_new_vms_and_builds(b):
    assert proxmox.tf_placement(b)["vlan"] == "" and proxmox.packer_vars(b)["PKR_VAR_proxmox_vlan"] == ""
    backends.update(b, {"proxmox_vlan": " 120 "}, "test")
    tagged = backends.get(b.id)
    assert tagged.cfg.proxmox_vlan == "120"
    assert proxmox.tf_placement(tagged)["vlan"] == "120" and proxmox.packer_vars(tagged)["PKR_VAR_proxmox_vlan"] == "120"
    assert "vlan" in proxmox.TF_PLACEMENT_VARS                          # so backups accept it in a VM's variables


@pytest.mark.parametrize("bad", ["0", "4095", "-1", "ten", "1.5"])
def test_vlan_tag_is_validated(bad):
    with pytest.raises(ValueError, match="1 to 4094"):
        config.BackendFields(proxmox_vlan=bad)


def test_the_modules_take_the_vlan():
    main = (config.APP_DIR.parent / "terraform" / "proxmox" / "main.tf").read_text()
    assert 'vlan_id = var.vlan != "" ? tonumber(var.vlan) : null' in main
    for image in (config.APP_DIR.parent / "images").glob("*/proxmox.pkr.hcl"):
        assert "vlan_tag = var.proxmox_vlan" in image.read_text(), image
        assert 'variable "proxmox_vlan"' in (image.parent / "variables.pkr.hcl").read_text(), image


# --- clusters with several nodes ----------------------------------------------------------

@pytest.fixture
def cluster(b, pve):
    """The same backend on a cluster where pve2 is online too and may take VMs."""
    pve.api["GET /nodes"] = [{"node": "pve2", "status": "online"}, {"node": "pve1", "status": "online"},
                             {"node": "pve3", "status": "offline"}]
    pve.api["POST /nodes/pve2/storage/local/upload"] = UPID
    pve.api[f"GET /nodes/pve2/tasks/{UPID}/status"] = {"status": "stopped", "exitstatus": "OK"}
    backends.update(b, {"proxmox_nodes": "pve2, pve3, pve1"}, "test")
    return backends.get(b.id)


def test_nodes_a_vm_can_go_on(b, cluster):
    assert proxmox.locations(b) == ["pve1"]                             # only its own node: nothing to choose
    assert cluster.cfg.proxmox_nodes == ["pve2", "pve3", "pve1"]
    assert proxmox.allowed_nodes(cluster) == ["pve1", "pve2", "pve3"]   # its own node first, once
    assert proxmox.locations(cluster) == ["pve1", "pve2"]               # pve3 is offline
    assert proxmox.tf_placement(cluster)["node"] == "pve1"
    assert proxmox.tf_placement(cluster, "pve2")["node"] == "pve2"
    with pytest.raises(ValueError, match="doesn't put VMs on node"):
        proxmox.tf_placement(cluster, "pve9")
    with pytest.raises(ValueError, match="not a node name"):
        config.BackendFields(proxmox_nodes="pve2, bad/name")


class JobLog:
    text = ""

    def step(self, t):
        self.text += t + "\n"

    def write(self, t):
        self.text += t


async def test_the_seed_goes_to_the_node_the_vm_goes_on(cluster, pve, data_dir):
    req = provision.VMRequest(name="new-02", template=TEMPLATE, cpus=2, memory_mb=2048, disk_gb=30,
                              ssh_keys=["ssh-ed25519 AAAAkey3 x@y"], backend_id=cluster.id, location="pve2")
    d = data_dir / "vms" / "new-02"
    d.mkdir(parents=True)
    extra = await proxmox.before_create(cluster, req, d, JobLog())
    assert ("POST", "/nodes/pve2/storage/local/upload") in pve.calls()
    assert ("POST", "/nodes/pve1/storage/local/upload") not in pve.calls()
    assert extra["template_node"] == "pve1"                             # cloned from where the template is
    assert provision.tfvars(req, cluster)["node"] == "pve2"


def state_on(node, vmid=101, serial=4):
    return {"version": 4, "serial": serial, "lineage": "x", "resources": [{
        "mode": "managed", "type": "proxmox_virtual_environment_vm", "name": "this",
        "provider": 'provider["registry.opentofu.org/bpg/proxmox"]',
        "instances": [{"attributes": {"vm_id": vmid, "id": str(vmid), "node_name": node, "name": "web-01"}}]}]}


async def test_a_migrated_vm_is_followed_before_a_change(cluster, pve, data_dir):
    """The provider talks to the VM through the node in its state; after a migration that's
    the wrong one, and the node in the variables would be a plan to move the VM back."""
    d = data_dir / "vms" / "web-01"
    d.mkdir(parents=True)
    (d / "terraform.tfstate").write_text(json.dumps(state_on("pve2")))
    (d / "terraform.tfvars.json").write_text(json.dumps({"node": "pve2", "name": "web-01"}))
    log = JobLog()
    await proxmox.before_change(cluster, d, log)                        # the fake has web-01 (101) on pve1
    state = json.loads((d / "terraform.tfstate").read_text())
    assert proxmox.state_node(state)[1] == "pve1" and state["serial"] == 5
    assert json.loads((d / "terraform.tfvars.json").read_text()) == {"node": "pve1", "name": "web-01"}
    assert "on node pve1 now" in log.text
    assert (d / "terraform.tfstate").stat().st_mode & 0o777 == 0o600

    before = (d / "terraform.tfstate").read_bytes()
    await proxmox.before_change(cluster, d, JobLog())                   # nothing moved: nothing written
    assert (d / "terraform.tfstate").read_bytes() == before

    (d / "terraform.tfstate").write_text(json.dumps(state_on("pve2", vmid=999)))
    await proxmox.before_change(cluster, d, JobLog())                   # not in the cluster: left for the plan
    assert proxmox.state_node(json.loads((d / "terraform.tfstate").read_text()))[1] == "pve2"


async def test_destroy_removes_the_seed_wherever_it_is(cluster, pve):
    await proxmox.after_destroy(cluster, "web-01", "101", JobLog())
    deleted = [p for m, p in pve.calls("DELETE")]
    for node in ("pve1", "pve2"):
        assert f"/nodes/{node}/storage/local/content/local:iso/vm-portal-seed-web-01-clean.iso" in deleted


def test_placement_says_what_local_storage_means():
    from app.routes_backends import proxmox_warnings
    def node(name, online=True, shared=False, bridges=("vmbr0",)):
        return {"name": name, "online": online, "bridges": list(bridges), "storages": [
            {"name": "local", "content": ["iso"], "shared": shared},
            {"name": "local-lvm", "content": ["images"], "shared": shared}]}
    chosen = {"proxmox_node": "pve1", "proxmox_storage": "local-lvm", "proxmox_iso_storage": "local",
              "proxmox_bridge": "vmbr0", "proxmox_nodes": "pve2, pve3, pve4, pve9"}
    assert proxmox_warnings({"nodes": [node("pve1")]}, chosen) == []    # one node: nothing to say
    inv = {"nodes": [node("pve1"), node("pve2"), node("pve3", online=False), node("pve4", bridges=("vmbr7",))]}
    text = " ".join(proxmox_warnings(inv, chosen))
    assert "won't migrate" in text and "cloned on the template's node" in text
    assert "Node pve3 is offline" in text and "Node pve4 has no bridge vmbr0" in text and "Node pve9 isn't in" in text
    assert "Node pve2" not in text
    shared = {"nodes": [node("pve1", shared=True), node("pve2", shared=True)]}
    assert proxmox_warnings(shared, {**chosen, "proxmox_nodes": ["pve2"]}) == []


def test_create_form_offers_the_nodes(cluster, monkeypatch):
    from app.platforms import vsphere
    monkeypatch.setattr(vsphere, "templates", lambda x: [])
    monkeypatch.setattr(vsphere, "vms_by_name", lambda x, fresh=False: {})
    with client() as c:
        token = login(c)
        page = c.get(f"/vms/new?backend={cluster.id}").text
        assert 'name="location"' in page and '<option value="pve2"' in page and "pve3" not in page
        assert 'name="location"' not in c.get("/vms/new").text          # the vCenter has nothing to choose
        r = c.post("/vms", data={"csrf_token": token, "name": "new-03", "template": TEMPLATE, "cpus": 2,
                                 "memory_mb": 2048, "disk_gb": 40, "backend_id": str(cluster.id),
                                 "location": "pve3", "ssh_keys": "ssh-ed25519 AAAAkey a@b"})
        assert r.status_code == 400 and "can&#39;t put a VM on node &#39;pve3&#39;" in r.text
    assert db.one("SELECT COUNT(*) AS n FROM vms")["n"] == 0


def test_vm_page_shows_the_node(b, monkeypatch):
    from app.platforms import vsphere
    monkeypatch.setattr(vsphere, "templates", lambda x: [])
    db.execute("INSERT INTO vms (name, status, template, cpus, memory_mb, disk_gb, ip_mode, created_at, backend_id)"
               " VALUES ('web-01', 'ready', 't', 1, 1024, 10, 'dhcp', ?, ?)", (db.now(), b.id))
    with client() as c:
        login(c)
        page = c.get("/vms/web-01").text
        assert "<dt>Node</dt><dd>pve1</dd>" in page
        assert "the QEMU guest agent" in page or "ssh " in page         # Proxmox's words, not VMware's
        assert "vCenter" not in page.split("<main")[1] and "VMware" not in page.split("<main")[1]


def test_placement_says_why_nothing_can_be_chosen():
    """A token without permissions gets empty lists from Proxmox, not an error."""
    from app.routes_backends import proxmox_access
    chosen = {"proxmox_node": "pve1"}
    bare = {"name": "pve1", "online": True, "storages": [], "bridges": [], "interfaces": ["nic0"]}
    text = " ".join(proxmox_access({"nodes": [bare], "privileges": []}, chosen))
    assert "no permissions" in text and "API Token Permission" in text

    some = ["VM.Audit", "Sys.Audit"]
    text = " ".join(proxmox_access({"nodes": [bare], "privileges": some,
                                    "missing_privileges": proxmox.missing_privileges(some)}, chosen))
    assert "lacks privileges" in text and "VM.Allocate" in text and "VM.Audit," not in text
    assert "sees no storage on node pve1" in text and "its interfaces: nic0" in text

    full = list(proxmox.PRIVILEGES) + ["VM.Monitor"]
    ok = {"name": "pve1", "online": True, "bridges": ["vmbr0"],
          "storages": [{"name": "local", "content": ["iso"]}, {"name": "lvm", "content": ["images"]}]}
    assert proxmox_access({"nodes": [ok], "privileges": full}, chosen) == []
    assert proxmox_access({"nodes": [ok], "privileges": None}, chosen) == []      # unknown: say nothing
    no_iso = {**ok, "storages": [{"name": "lvm", "content": ["images"]}]}
    assert "takes ISO images" in " ".join(proxmox_access({"nodes": [no_iso], "privileges": full}, chosen))


def test_inventory_reads_the_tokens_privileges(pve):
    creds = {k: getattr(pve.conn(), k) for k in ("server", "token_id", "token_secret", "fingerprint")}
    pve.api["GET /access/permissions"] = {"/": {"VM.Audit": 1}, "/storage/local": {"Datastore.Audit": 0}}
    inv = proxmox.inventory(**creds, fresh=True)
    assert inv["privileges"] == ["Datastore.Audit", "VM.Audit"]
    assert "VM.Allocate" in inv["missing_privileges"] and "VM.Audit" not in inv["missing_privileges"]
    pve.api["GET /access/permissions"] = {}
    assert proxmox.inventory(**creds, fresh=True)["privileges"] == []


def test_placement_dropdowns_explain_themselves(b):
    """A storage's free space is the line under its dropdown, and an empty dropdown says why."""
    from app.routes_backends import proxmox_placement
    chosen = {k: "" for k in proxmox.PLACEMENT} | {"proxmox_node": "pve1", "proxmox_nodes": []}
    st = {"name": "lvm", "type": "lvmthin", "content": ["images"], "free_gb": 40, "capacity_gb": 100, "shared": False}
    inv = {"nodes": [{"name": "pve1", "online": True, "storages": [st], "bridges": []}], "pools": []}
    by_key = {f["key"]: f for f in proxmox_placement(b, inv, chosen)}
    disk, iso, bridge = by_key["proxmox_storage"], by_key["proxmox_iso_storage"], by_key["proxmox_bridge"]
    assert disk["options"] == [("lvm", "lvm")] and disk["details"] == {"lvm": "40 GB free of 100 GB (lvmthin)"}
    assert iso["empty"] == "No storage for ISO images found" and "API token can see" in iso["hint"]
    assert bridge["empty"] == "No bridge found"
    unchosen = {f["key"]: f for f in proxmox_placement(b, {**inv, "nodes": inv["nodes"] * 1 + [
        {"name": "pve2", "online": True, "storages": [], "bridges": []}]}, chosen | {"proxmox_node": ""})}
    assert unchosen["proxmox_storage"]["empty"] == "Choose a node first" and not unchosen["proxmox_storage"]["hint"]
