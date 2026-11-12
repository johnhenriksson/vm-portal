import asyncio
import os

import pytest
import uvloop

# Production runs on uvloop (uvicorn[standard]); test on it too. It differs from asyncio's
# own loop in places (e.g. subprocess options), and a job broke on exactly that once.
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

os.environ.update({
    "ADMIN_PASSWORD": "test-password-123",
    "SECRET_KEY": "x" * 40,
    "VSPHERE_SERVER": "vc.example", "VSPHERE_USER": "u", "VSPHERE_PASSWORD": "vc-secret",
    "VSPHERE_DATACENTER": "DC", "VSPHERE_CLUSTER": "C", "VSPHERE_DATASTORE": "DS", "VSPHERE_NETWORK": "N",
    "IP_POOL_START": "10.0.0.10", "IP_POOL_END": "10.0.0.14",
    "IP_GATEWAY": "10.0.0.1", "IP_PREFIX": "24", "IP_DNS": "10.0.0.1,1.1.1.1",
    "DEFAULT_SSH_KEYS": "ssh-ed25519 AAAAkey1 a@b|ssh-ed25519 AAAAkey2 c@d",
    "TEMPLATE_PREFIXES": "ubuntu-2604-docker, ubnt-packer",
})


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    from app import auth, config, db, jobs, power, users
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # don't pick up a developer's .env
    config.reset_caches()
    db._conn = None
    users.clear_throttles()
    users.setup_token = None
    auth.reset_session_epoch()
    jobs.paused = None
    power.clear()
    yield tmp_path
    power.clear()
    config.reset_caches()
    db._conn = None
    auth.reset_session_epoch()
