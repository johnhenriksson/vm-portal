import pytest

from app import db, ipam
from app.platforms import vsphere

ARP = """IP address       HW type     Flags       HW address            Mask     Device
10.0.0.12        0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0
10.0.0.13        0x1         0x0         00:00:00:00:00:00     *        eth0
"""


def test_pool():
    assert [str(i) for i in ipam.pool()] == [f"10.0.0.{i}" for i in range(10, 15)]


def test_parse_arp_ignores_incomplete():
    assert ipam.parse_arp(ARP) == {"10.0.0.12"}


def test_classify_reasons_and_free():
    r = ipam.classify(
        [f"10.0.0.{i}" for i in range(10, 15)],
        reserved={"10.0.0.10"}, vcenter={"10.0.0.11"}, alive=set(), arp={"10.0.0.12"}, infra={"10.0.0.1"},
    )
    assert r.free == ["10.0.0.13", "10.0.0.14"]
    assert r.used == {"10.0.0.10": "assigned by portal", "10.0.0.11": "in use by a vCenter VM",
                      "10.0.0.12": "seen in ARP table"}


@pytest.mark.parametrize("ip,msg", [
    ("nope", "not a valid"), ("192.168.1.5", "not in"), ("10.0.0.255", "broadcast"), ("10.0.0.0", "network"),
])
def test_validate_static_ip_rejects(ip, msg):
    with pytest.raises(ValueError, match=msg):
        ipam.validate_static_ip(ip)


def test_validate_static_ip_ok():
    assert ipam.validate_static_ip(" 10.0.0.50 ") == "10.0.0.50"


async def test_scan_combines_sources(monkeypatch):
    db.execute("INSERT INTO vms (name,status,template,cpus,memory_mb,disk_gb,ip_mode,ip,created_at)"
               " VALUES ('a','ready','t',1,1,1,'static','10.0.0.10','now')")
    db.execute("INSERT INTO vms (name,status,template,cpus,memory_mb,disk_gb,ip_mode,ip,created_at)"
               " VALUES ('b','destroyed','t',1,1,1,'static','10.0.0.11','now')")
    monkeypatch.setattr(vsphere, "used_ips", lambda: {"10.0.0.14"})

    async def fake_ping(ips):
        return {"10.0.0.12"} & set(ips)
    monkeypatch.setattr(ipam, "ping_alive", fake_ping)
    monkeypatch.setattr(ipam, "arp_seen", lambda: set())

    r = await ipam.ScanIPAM().scan()
    assert r.free == ["10.0.0.11", "10.0.0.13"]  # destroyed VM's IP is free again
    assert await ipam.ScanIPAM().check("10.0.0.12") == "answers ping"
    assert await ipam.ScanIPAM().check("10.0.0.13") is None
