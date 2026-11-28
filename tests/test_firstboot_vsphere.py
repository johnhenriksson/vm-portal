"""vSphere's first-boot result: the guest's reporter (cloud-init user-data), its report, and
the portal's reader of it in the VM's advanced settings."""
import json
import os
import subprocess
import sys

import pytest
import yaml
from pyVmomi import vim

from app import firstboot, provision
from app.guests import cloudinit
from app.platforms import PLATFORMS, vsphere
from app.provision import VMRequest

from backup_helpers import vc


def req(**kw):
    base = dict(name="app01", template="t", cpus=2, memory_mb=4096, disk_gb=40)
    return VMRequest(**{**base, **kw})


# --- the request and its user-data -------------------------------------------------------

def test_only_vsphere_asks_the_guest_to_report():
    assert provision.prepared(req(), vc()).firstboot_report == "guestinfo"

    class Pve:
        platform = PLATFORMS["proxmox"]
    assert provision.prepared(req(), Pve).firstboot_report is None
    assert firstboot.tracked(vsphere)


def test_userdata_carries_the_reporter_first():
    ud = yaml.safe_load(provision.render_userdata(req(firstboot_report="guestinfo",
                                                      compose_yaml="services:\n  a:\n    image: nginx\n")))
    paths = [f["path"] for f in ud["write_files"]]
    assert paths == [cloudinit.REPORT_SCRIPT, f"/etc/systemd/system/{cloudinit.REPORT_UNIT}", "/opt/app/compose.yaml"]
    assert ud["runcmd"][:2] == [["systemctl", "daemon-reload"],
                                ["systemctl", "start", "--no-block", cloudinit.REPORT_UNIT]]
    assert ud["runcmd"][2][0] == "docker"
    unit = ud["write_files"][1]["content"]
    assert "After=cloud-final.service" in unit and "[Install]" not in unit   # once, never at every boot


def test_userdata_without_reporter_is_unchanged():
    ud = yaml.safe_load(provision.render_userdata(req()))
    assert "write_files" not in ud and "runcmd" not in ud


# --- the reporter, run for real against stand-in files ----------------------------------

def run_reporter(tmp_path, result: str | None, log: str = ""):
    res, out_log, sent = tmp_path / "result.json", tmp_path / "output.log", tmp_path / "sent"
    if result is not None:
        res.write_text(result)
    out_log.write_text(log)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = bindir / "vmware-rpctool"
    tool.write_text(f"#!/bin/sh\nprintf '%s' \"$1\" > {sent}\n")
    tool.chmod(0o755)
    script = (cloudinit._REPORTER.replace('"/run/cloud-init/result.json"', repr(str(res)))
              .replace('"/var/log/cloud-init-output.log"', repr(str(out_log)))
              .replace("range(120)", "range(1)").replace("time.sleep(5)", "pass"))
    p = subprocess.run([sys.executable, "-I", "-c", script], env={**os.environ, "PATH": str(bindir)},
                       capture_output=True, text=True)
    return p, (sent.read_text() if sent.exists() else None)


def report_of(sent: str) -> str:
    prefix = f"info-set {cloudinit.REPORT_KEY} "
    assert sent.startswith(prefix)
    return sent[len(prefix):]


def test_reporter_sends_success_without_log(tmp_path):
    p, sent = run_reporter(tmp_path, '{"v1": {"errors": []}}', log="secret-ish line\n")
    assert p.returncode == 0, p.stderr
    assert json.loads(report_of(sent)) == {"v": 1, "errors": [], "log": ""}
    assert firstboot.parse_report(report_of(sent)).ok


def test_reporter_sends_errors_and_the_log_tail(tmp_path):
    log = "\n".join(f"line {i}" for i in range(100))
    p, sent = run_reporter(tmp_path, json.dumps({"v1": {"errors": ["runcmd failed"]}}), log=log)
    assert p.returncode == 0, p.stderr
    r = firstboot.parse_report(report_of(sent))
    assert not r.ok and r.errors == ["runcmd failed"]
    assert r.log.splitlines() == [f"line {i}" for i in range(80, 100)]


def test_reporter_shrinks_a_huge_report_but_keeps_it_valid(tmp_path):
    errors = ["é" * 2000] * 50          # escaped as \u00e9: six characters each
    log = "\n".join("ü" * 1000 for _ in range(50))
    p, sent = run_reporter(tmp_path, json.dumps({"v1": {"errors": errors}}), log=log)
    assert p.returncode == 0, p.stderr
    text = report_of(sent)
    assert len(text) <= 12000
    assert not firstboot.parse_report(text).ok


def test_reporter_sends_nothing_without_a_result(tmp_path):
    p, sent = run_reporter(tmp_path, None)
    assert p.returncode != 0 and sent is None


# --- the portal's side -------------------------------------------------------------------

def test_parse_report_rejects_what_it_doesnt_know():
    for bad in ("not json", "[]", '{"v": 2, "errors": []}', '{"v": 1, "errors": []}' + " " * firstboot.REPORT_MAX):
        with pytest.raises(ValueError):
            firstboot.parse_report(bad)
    r = firstboot.parse_report(json.dumps({"v": 1, "errors": "one\x1b[31m", "log": "a\x00b", "extra": 1}))
    assert r.errors == ["one?[31m"] and r.log == "a?b"


class Task:
    class info:
        state = vim.TaskInfo.State.success


class FakeVM:
    def __init__(self, extra, fail=False):
        self.config = type("C", (), {"extraConfig": [vim.option.OptionValue(key=k, value=v) for k, v in extra.items()]})
        self.specs, self.fail = [], fail

    def ReconfigVM_Task(self, spec):
        if self.fail:
            raise vim.fault.NoPermission()
        self.specs.append(spec)
        return Task()


@pytest.fixture
def found(monkeypatch):
    holder = {}
    monkeypatch.setattr(vsphere, "_find_vm_locked", lambda b, name, moid: holder["vm"])
    return holder


def test_reader_waits_for_a_report(found):
    found["vm"] = FakeVM({"guestinfo.userdata": "x", cloudinit.REPORT_KEY: ""})
    assert vsphere.first_boot(vc(), "app01", "vm-1") is None
    assert found["vm"].specs == []


def test_reader_returns_the_report_and_blanks_it(found):
    found["vm"] = FakeVM({cloudinit.REPORT_KEY: json.dumps({"v": 1, "errors": ["boom"], "log": "last"})})
    r = vsphere.first_boot(vc(), "app01", "vm-1")
    assert not r.ok and r.errors == ["boom"] and r.log == "last"
    (spec,) = found["vm"].specs
    assert [(o.key, o.value) for o in spec.extraConfig] == [(cloudinit.REPORT_KEY, "")]


def test_reader_keeps_the_result_when_it_cant_blank_it(found):
    found["vm"] = FakeVM({cloudinit.REPORT_KEY: '{"v": 1, "errors": []}'}, fail=True)
    assert vsphere.first_boot(vc(), "app01", "vm-1").ok


# As on a real Debian 13 VM: SSH host key output lands after cloud-init's "finished" line.
REAL_LOG = "\n".join([
    "Cloud-init v. 25.1.4 running 'init' at Sat, 28 Nov 2026 08:28:12 +0000. Up 18.55 seconds.",
    *[f"ci-info: row {i}" for i in range(30)],
    "Cloud-init v. 25.1.4 running 'modules:final' at Sat, 28 Nov 2026 08:28:18 +0000. Up 24.02 seconds.",
    "Error response from daemon: failed to resolve reference \"registry.invalid/x:1\"",
    "2026-11-28 08:28:19,718 - cc_scripts_user.py[WARNING]: Failed to run module scripts-user",
    "Cloud-init v. 25.1.4 finished at Sat, 28 Nov 2026 08:28:19 +0000. Datasource DataSourceVMware.  Up 25.64 seconds",
    "Generating public/private rsa key pair.",
    *[f"|randomart {i}|" for i in range(40)],
])


def test_reporter_log_ends_where_cloud_init_finished(tmp_path):
    p, sent = run_reporter(tmp_path, json.dumps({"v1": {"errors": ["scripts-user failed"]}}), log=REAL_LOG)
    assert p.returncode == 0, p.stderr
    lines = firstboot.parse_report(report_of(sent)).log.splitlines()
    assert len(lines) == 20 and lines[-1].startswith("Cloud-init v. 25.1.4 finished")
    assert any("Error response from daemon" in line for line in lines)
    assert not any("randomart" in line for line in lines)


def test_log_excerpt_without_a_finished_line_is_the_tail():
    text = "\n".join(f"l{i}" for i in range(50))
    assert firstboot.log_excerpt(text).splitlines() == [f"l{i}" for i in range(30, 50)]
    assert "randomart" not in firstboot.log_excerpt(REAL_LOG)
