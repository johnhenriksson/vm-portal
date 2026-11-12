"""Find free static IPs.

Each backend has its own pool, gateway and DNS. An IP in a backend's pool counts as
*used* if any of these say so:
  - the portal already assigned it to a VM (database, any backend)
  - any backend reports it on a VM (VMware Tools, the QEMU guest agent, cloud-init settings)
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

from . import backends, db
from .config import get_settings

log = logging.getLogger(__name__)

ARP_TABLE = Path("/proc/net/arp")


@dataclass
class ScanResult:
    free: list[str] = field(default_factory=list)
    used: dict[str, str] = field(default_factory=dict)  # ip -> reason
    scanned_at: float = 0.0


class IPAMProvider(Protocol):
    async def scan(self, b, fresh: bool = False) -> ScanResult: ...
    async def check(self, b, ip: str) -> str | None: ...


class PoolNotConfigured(Exception):
    pass


def pool(b) -> list[ipaddress.IPv4Address]:
    s = b.cfg
    if not (s.ip_pool_start and s.ip_pool_end):
        raise PoolNotConfigured(f"Set {b.name}'s IP pool (Settings → Backends) to use static IPs")
    start, end = ipaddress.IPv4Address(s.ip_pool_start), ipaddress.IPv4Address(s.ip_pool_end)
    if end < start:
        raise PoolNotConfigured("IP_POOL_END is before IP_POOL_START")
    return [ipaddress.IPv4Address(i) for i in range(int(start), int(end) + 1)]


def network(b) -> ipaddress.IPv4Network:
    s = b.cfg
    return ipaddress.IPv4Network(f"{s.ip_gateway}/{s.ip_prefix}", strict=False)


def validate_static_ip(b, ip: str) -> str:
    """Return the normalized IP or raise ValueError with a user-facing message."""
    try:
        addr = ipaddress.IPv4Address(ip.strip())
    except ValueError:
        raise ValueError(f"{ip!r} is not a valid IPv4 address") from None
    if not b.cfg.ip_gateway:
        raise ValueError(f"{b.name} has no gateway set (Settings → Backends), so static IPs are off there")
    net = network(b)
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


def classify(candidates: list[str], *, reserved: set[str], on_backends: dict[str, str] | set[str],
             alive: set[str], arp: set[str], infra: set[str]) -> ScanResult:
    """on_backends: the addresses backends report, with the backend's name (or a plain set)."""
    result = ScanResult(scanned_at=time.time())
    for ip in candidates:
        if ip in infra:
            result.used[ip] = "gateway/DNS"
        elif ip in reserved:
            result.used[ip] = "assigned by portal"
        elif ip in on_backends:
            where = on_backends.get(ip) if isinstance(on_backends, dict) else None
            result.used[ip] = f"in use by a VM on {where}" if where else "in use by a VM"
        elif ip in alive:
            result.used[ip] = "answers ping"
        elif ip in arp:
            result.used[ip] = "seen in ARP table"
        else:
            result.free.append(ip)
    return result


async def platform_ips() -> dict[str, str]:
    """Every address any backend reports on a VM, with that backend's name. Backends often
    share a network, so a pool is checked against all of them. One that can't be reached
    is skipped (the ping and ARP checks still run)."""
    async def one(b):
        try:
            return b, await asyncio.to_thread(b.platform.used_ips, b)
        except Exception:  # noqa: BLE001 - scanning still works without this backend's data
            log.exception("could not read VM IPs from backend %s", b.name)
            return b, set()

    found: dict[str, str] = {}
    for b, ips in await asyncio.gather(*(one(b) for b in backends.all_backends() if b.configured)):
        for ip in ips:
            found.setdefault(ip, b.name)
    return found


class ScanIPAM:
    CACHE_SECONDS = 60

    def __init__(self) -> None:
        self._cache: dict[int, ScanResult] = {}
        self._lock = asyncio.Lock()

    async def scan(self, b, fresh: bool = False) -> ScanResult:
        async with self._lock:
            hit = self._cache.get(b.id)
            if not fresh and hit and time.time() - hit.scanned_at < self.CACHE_SECONDS:
                return hit
            s = b.cfg
            candidates = [str(i) for i in pool(b)]
            on_backends = await platform_ips()
            alive = await ping_alive(candidates)
            self._cache[b.id] = classify(
                candidates,
                reserved=db.reserved_ips(),
                on_backends=on_backends,
                alive=alive,
                arp=arp_seen(),
                infra={s.ip_gateway, *s.ip_dns},
            )
            return self._cache[b.id]

    async def check(self, b, ip: str) -> str | None:
        """Re-check one IP right before using it. Returns why it's in use, or None."""
        s = b.cfg
        if ip in {s.ip_gateway, *s.ip_dns}:
            return "gateway/DNS"
        if ip in db.reserved_ips():
            return "assigned by portal"
        where = (await platform_ips()).get(ip)
        if where:
            return f"in use by a VM on {where}"
        if await ping_alive([ip]):
            return "answers ping"
        if ip in arp_seen():
            return "seen in ARP table"
        return None


ipam: IPAMProvider = ScanIPAM()
