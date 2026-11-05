"""Find free static IPs.

An IP in the pool counts as *used* if any of these say so:
  - the portal already assigned it to a VM (database)
  - vCenter reports it on any VM (VMware Tools)
  - it answers ping
  - it appears in the kernel ARP table after the ping sweep (catches hosts that
    drop ICMP; only meaningful when the container uses host networking)
  - it is the gateway or a DNS server

The provider interface is small so a NetBox provider can replace this later.
"""
import asyncio
import ipaddress
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import db, vsphere
from .config import get_settings

log = logging.getLogger(__name__)

ARP_TABLE = Path("/proc/net/arp")


@dataclass
class ScanResult:
    free: list[str] = field(default_factory=list)
    used: dict[str, str] = field(default_factory=dict)  # ip -> reason
    scanned_at: float = 0.0


class IPAMProvider(Protocol):
    async def scan(self, fresh: bool = False) -> ScanResult: ...
    async def check(self, ip: str) -> str | None: ...


class PoolNotConfigured(Exception):
    pass


def pool() -> list[ipaddress.IPv4Address]:
    s = get_settings()
    if not (s.ip_pool_start and s.ip_pool_end):
        raise PoolNotConfigured("Set the IP pool (Settings → Network) to use static IPs")
    start, end = ipaddress.IPv4Address(s.ip_pool_start), ipaddress.IPv4Address(s.ip_pool_end)
    if end < start:
        raise PoolNotConfigured("IP_POOL_END is before IP_POOL_START")
    return [ipaddress.IPv4Address(i) for i in range(int(start), int(end) + 1)]


def network() -> ipaddress.IPv4Network:
    s = get_settings()
    return ipaddress.IPv4Network(f"{s.ip_gateway}/{s.ip_prefix}", strict=False)


def validate_static_ip(ip: str) -> str:
    """Return the normalized IP or raise ValueError with a user-facing message."""
    try:
        addr = ipaddress.IPv4Address(ip.strip())
    except ValueError:
        raise ValueError(f"{ip!r} is not a valid IPv4 address") from None
    s = get_settings()
    if not s.ip_gateway:
        raise ValueError("The gateway is not configured (Settings → Network); static IPs are disabled")
    net = network()
    if addr not in net:
        raise ValueError(f"{addr} is not in {net}")
    if addr in (net.network_address, net.broadcast_address):
        raise ValueError(f"{addr} is the network or broadcast address")
    return str(addr)


async def ping_alive(ips: list[str]) -> set[str]:
    if not ips:
        return set()
    if shutil.which("fping"):
        proc = await asyncio.create_subprocess_exec(
            "fping", "-a", "-q", "-r", "1", "-t", "300", *ips,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()  # exit code 1 just means some hosts are down
        return {line.strip() for line in out.decode().splitlines() if line.strip()}

    sem = asyncio.Semaphore(64)

    async def one(ip: str) -> str | None:
        async with sem:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "1", "-W", "1", "-n", ip,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            return ip if await proc.wait() == 0 else None

    return {ip for ip in await asyncio.gather(*(one(i) for i in ips)) if ip}


def parse_arp(text: str) -> set[str]:
    """IPs with a resolved MAC in /proc/net/arp."""
    found = set()
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[2] != "0x0" and parts[3] != "00:00:00:00:00:00":
            found.add(parts[0])
    return found


def arp_seen() -> set[str]:
    if not get_settings().ip_scan_use_arp:
        return set()
    try:
        return parse_arp(ARP_TABLE.read_text())
    except OSError:
        return set()


def classify(candidates: list[str], *, reserved: set[str], vcenter: set[str],
             alive: set[str], arp: set[str], infra: set[str]) -> ScanResult:
    result = ScanResult(scanned_at=time.time())
    for ip in candidates:
        if ip in infra:
            result.used[ip] = "gateway/DNS"
        elif ip in reserved:
            result.used[ip] = "assigned by portal"
        elif ip in vcenter:
            result.used[ip] = "in use by a vCenter VM"
        elif ip in alive:
            result.used[ip] = "answers ping"
        elif ip in arp:
            result.used[ip] = "seen in ARP table"
        else:
            result.free.append(ip)
    return result


class ScanIPAM:
    CACHE_SECONDS = 60

    def __init__(self) -> None:
        self._cache: ScanResult | None = None
        self._lock = asyncio.Lock()

    async def scan(self, fresh: bool = False) -> ScanResult:
        async with self._lock:
            if not fresh and self._cache and time.time() - self._cache.scanned_at < self.CACHE_SECONDS:
                return self._cache
            s = get_settings()
            candidates = [str(i) for i in pool()]
            try:
                vcenter = await asyncio.to_thread(vsphere.used_ips)
            except Exception:  # noqa: BLE001 - scanning still works without vCenter data
                log.exception("could not read VM IPs from vCenter")
                vcenter = set()
            alive = await ping_alive(candidates)
            self._cache = classify(
                candidates,
                reserved=db.reserved_ips(),
                vcenter=vcenter,
                alive=alive,
                arp=arp_seen(),
                infra={s.ip_gateway, *s.ip_dns},
            )
            return self._cache

    async def check(self, ip: str) -> str | None:
        """Re-check one IP right before using it. Returns why it's in use, or None."""
        s = get_settings()
        if ip in {s.ip_gateway, *s.ip_dns}:
            return "gateway/DNS"
        if ip in db.reserved_ips():
            return "assigned by portal"
        try:
            if ip in await asyncio.to_thread(vsphere.used_ips):
                return "in use by a vCenter VM"
        except Exception:  # noqa: BLE001
            log.exception("could not read VM IPs from vCenter")
        if await ping_alive([ip]):
            return "answers ping"
        if ip in arp_seen():
            return "seen in ARP table"
        return None


ipam: IPAMProvider = ScanIPAM()
