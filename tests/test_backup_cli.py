import fcntl
import os

import pytest

from app import config, db, users
from app.backup.__main__ import main
from backup_helpers import make_vm

PASS = "a long enough cli passphrase"


@pytest.fixture
def passfile(tmp_path_factory):
    p = tmp_path_factory.mktemp("secrets") / "pass"
    p.write_text(PASS + "\n")
    p.chmod(0o600)
    return str(p)


def test_keygen_writes_a_private_file_and_never_overwrites(tmp_path, capsys):
    out = tmp_path / "key.txt"
    assert main(["keygen", "--out", str(out)]) == 0
    public = capsys.readouterr().out.strip()
    assert public.startswith("age1") and out.stat().st_mode & 0o777 == 0o600
    assert "AGE-SECRET-KEY-1" in out.read_text() and public in out.read_text()
    assert main(["keygen", "--out", str(out)]) == 1   # exists: refused


def test_create_verify_restore_undo(tmp_path, passfile, capsys):
    users.create("boss", "correct horse battery", "admin")
    make_vm()
    out = str(tmp_path / "b.vmpbak")
    assert main(["create", "--out", out, "--passphrase-file", passfile]) == 0
    assert os.stat(out).st_mode & 0o777 == 0o600
    assert main(["verify", out, "--passphrase-file", passfile]) == 0
    assert "can be restored" in capsys.readouterr().out
    users.create("carol", "correct horse battery", "user")
    assert main(["restore", out, "--passphrase-file", passfile, "--yes"]) == 0
    assert users.by_name("carol") is None and db.active_vm("web-01")
    assert main(["undo-restore", "--yes"]) == 0
    assert users.by_name("carol") is not None


def test_vm_backup_and_public_keys(tmp_path, capsys):
    make_vm()
    key = tmp_path / "key.txt"
    main(["keygen", "--out", str(key)])
    public = capsys.readouterr().out.strip()
    out = str(tmp_path / "vms.vmpbak")
    assert main(["create", "--out", out, "--vm", "web-01", "--recipient", public]) == 0
    assert main(["verify", out, "--identity-file", str(key)]) == 0
    assert "web-01: state OK" in capsys.readouterr().out
    assert main(["verify", out, "--passphrase-file", str(key)]) == 1  # not a passphrase backup


def test_restore_refuses_while_the_portal_runs(tmp_path, passfile):
    users.create("boss", "correct horse battery", "admin")
    out = str(tmp_path / "b.vmpbak")
    assert main(["create", "--out", out, "--passphrase-file", passfile]) == 0
    # Another process (the running portal) holds the data directory lock.
    fd = os.open(config.get_bootstrap().data_dir / ".portal.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        for argv in (["restore", out, "--passphrase-file", passfile, "--yes"], ["undo-restore", "--yes"],
                     ["create", "--out", str(tmp_path / "c.vmpbak"), "--passphrase-file", passfile]):
            with pytest.raises(SystemExit, match="in use by another process"):
                main(argv)
        assert main(["create", "--out", str(tmp_path / "c.vmpbak"), "--passphrase-file", passfile,
                     "--allow-running"]) == 0
    finally:
        os.close(fd)


def test_restore_needs_confirmation_without_yes(tmp_path, passfile, monkeypatch):
    users.create("boss", "correct horse battery", "admin")
    out = str(tmp_path / "b.vmpbak")
    main(["create", "--out", out, "--passphrase-file", passfile])
    with pytest.raises(SystemExit, match="--yes"):
        main(["restore", out, "--passphrase-file", passfile])   # stdin isn't a terminal in tests
