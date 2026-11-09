import gzip
import io
import json
import tarfile

import pyrage
import pytest

from app.backup import archive
from app.backup.archive import BackupError


@pytest.fixture(scope="module")
def keypair():
    ident = pyrage.x25519.Identity.generate()
    return str(ident), str(ident.to_public())


def raw_tar(entries, manifest=None):
    """A tar.gz built by hand, for files a real backup never contains."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz, tarfile.open(fileobj=gz, mode="w") as tf:
        if manifest is not None:
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for info, data in entries:
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def manifest_for(files):
    return {"format": archive.FORMAT, "version": archive.VERSION, "kind": "vms",
            "files": {n: {"size": len(d), "sha256": archive.sha256(d)} for n, d in files.items()}}


def test_round_trip_with_public_key(keypair):
    secret, public = keypair
    files = {"db/vms.json": b"[]", "vms/web-01/terraform.tfstate": b"{}"}
    data = archive.write("vms", files, recipients=[public], installation_id="abc")
    assert data.startswith(archive.AGE_HEADER) and b"tfstate" not in data
    assert not archive.encrypted_to_passphrase(data)
    b = archive.read(data, secret)
    assert b.kind == "vms" and b.files == files and b.manifest["installation_id"] == "abc"


def test_passphrase_round_trip_and_wrong_passphrase():
    data = archive.write("full", {"secrets.json": b'{"vsphere_password": "a-stored-vcenter-password"}'},
                         passphrase="correct horse battery staple")
    assert archive.encrypted_to_passphrase(data) and b"a-stored-vcenter-password" not in data
    assert archive.read(data, "correct horse battery staple").json("secrets.json") == {"vsphere_password": "a-stored-vcenter-password"}
    with pytest.raises(BackupError, match="wrong passphrase"):
        archive.read(data, "wrong horse battery staple!!")
    with pytest.raises(ValueError, match="at least 16"):
        archive.write("full", {}, passphrase="short")


def test_there_is_no_unencrypted_backup():
    with pytest.raises(ValueError):
        archive.write("full", {})
    with pytest.raises(BackupError, match="not an age-encrypted"):
        archive.read(archive.pack("full", {}), "x" * 20)


def test_tampering_is_detected(keypair):
    secret, public = keypair
    data = bytearray(archive.write("vms", {"db/vms.json": b"[]"}, recipients=[public]))
    data[-20] ^= 1
    with pytest.raises(BackupError, match="changed"):
        archive.read(bytes(data), secret)


def test_only_allowed_paths(keypair):
    with pytest.raises(ValueError):
        archive.pack("full", {"../etc/passwd": b""})
    for bad in ("../../etc/cron.d/x", "/etc/passwd", "vms/web-01/../../x", "vms/Web/terraform.tfstate",
                "images/x/.hidden", "portal.db", "encryption.key", "session.key", "packer_ssh_key"):
        info = tarfile.TarInfo(bad)
        info.size = 1
        blob = raw_tar([(info, b"x")], manifest_for({bad: b"x"}))
        with pytest.raises(BackupError, match="not something a backup contains"):
            archive.unpack(blob)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.CHRTYPE])
def test_no_links_or_special_files(kind):
    info = tarfile.TarInfo("db/vms.json")
    info.type, info.linkname = kind, "/etc/shadow"
    with pytest.raises(BackupError, match="only regular files"):
        archive.unpack(raw_tar([(info, None)], manifest_for({})))


def test_manifest_must_match_exactly():
    good = {"db/vms.json": b"[]"}
    info = tarfile.TarInfo("db/vms.json")
    info.size = 2
    ok = raw_tar([(info, b"[]")], manifest_for(good))
    assert archive.unpack(ok).files == good
    # A file the manifest doesn't list (smuggled in), one it lists but is missing, a changed one.
    with pytest.raises(BackupError, match="don't match"):
        archive.unpack(raw_tar([(info, b"[]")], manifest_for({})))
    with pytest.raises(BackupError, match="don't match"):
        archive.unpack(raw_tar([], manifest_for(good)))
    with pytest.raises(BackupError, match="checksum"):
        archive.unpack(raw_tar([(info, b"{}")], manifest_for(good)))
    with pytest.raises(BackupError, match="newer portal"):
        archive.unpack(raw_tar([(info, b"[]")], {**manifest_for(good), "version": archive.VERSION + 1}))
    with pytest.raises(BackupError, match="no manifest"):
        archive.unpack(raw_tar([(info, b"[]")]))


def test_size_limits():
    info = tarfile.TarInfo("jobs/1.log")
    info.size = 2000
    blob = raw_tar([(info, b"x" * 2000)], manifest_for({"jobs/1.log": b"x" * 2000}))
    assert archive.unpack(blob, max_bytes=4000)
    with pytest.raises(BackupError, match="size limit"):
        archive.unpack(blob, max_bytes=1000)  # checked on the declared sizes, before reading


def test_archive_carries_no_host_details(keypair):
    blob = archive.pack("vms", {"db/vms.json": b"[]"})
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for m in tf:
            assert (m.mtime, m.uid, m.gid, m.uname, m.gname, m.mode) == (0, 0, 0, "", "", 0o600)
