"""Shared helpers for the backup tests: a VM with OpenTofu state, as the portal makes it."""
import json

from app import db, provision, users

PROVIDER = 'provider["registry.opentofu.org/vmware/vsphere"]'


def state(name="web-01", moid="vm-42", extra=()):
    resources = [
        {"mode": "data", "type": "vsphere_datacenter", "name": "this", "provider": PROVIDER,
         "instances": [{"attributes": {"id": "datacenter-1"}}]},
        {"mode": "managed", "type": "vsphere_virtual_machine", "name": "this", "provider": PROVIDER,
         "instances": [{"attributes": {"name": name, "moid": moid}}]},
        *extra,
    ]
    return json.dumps({"version": 4, "terraform_version": "1.10.0", "resources": resources}).encode()


def tfvars(name="web-01", **extra):
    return json.dumps({"datacenter": "DC", "cluster": "C", "datastore": "DS", "network": "N", "folder": "",
                       "template": "ubuntu-2604-docker-20261101-0300", "name": name, "cpus": 2,
                       "memory_mb": 4096, "disk_gb": 40, "metadata": "m", "userdata": "#cloud-config\n",
                       "annotation": "Created by vm-portal from ubuntu-2604-docker-20261101-0300", **extra}).encode()


def make_vm(name="web-01", moid="vm-42", owner="alice", ip="10.0.0.11"):
    if not users.by_name(owner):
        users.create(owner, "correct horse battery", "user")
    uid = users.by_name(owner)["id"]
    db.execute("INSERT INTO vms (name, status, template, cpus, memory_mb, disk_gb, ip_mode, ip, has_compose,"
               " created_at, owner_id, notes) VALUES (?, 'ready', 't', 2, 4096, 40, 'static', ?, 0, ?, ?, 'hi')",
               (name, ip, db.now(), uid))
    d = provision.vm_dir(name)
    d.mkdir(parents=True)
    (d / "terraform.tfstate").write_bytes(state(name, moid))
    (d / "terraform.tfvars.json").write_bytes(tfvars(name, vsphere_server="old.example"))
    return d


def admin():
    if not users.by_name("admin2"):
        users.create("admin2", "correct horse battery", "admin")
    return users.by_name("admin2")


def good_facts(name="web-01"):
    return {"name": name, "template": False, "annotation": "Created by vm-portal from x",
            "datacenter": "DC", "folder": ""}
