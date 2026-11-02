"""Resolve the real client IP behind the ingress.

The ingress forwards the real client address in X-Forwarded-For and trusts only
the front proxy, so the header is trustworthy only when the immediate peer is an
ingress pod. A direct pod-to-pod caller keeps its own address, and a header from
an untrusted peer is ignored. The rightmost untrusted hop is the client: trusted
proxies are stripped from the right, exactly as nginx's real_ip_recursive does.
"""

import ipaddress
from functools import lru_cache

from fastapi import Request

from app.config import settings

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=8)
def _networks(cidrs: str) -> tuple[Network, ...]:
    return tuple(
        ipaddress.ip_network(cidr.strip(), strict=False)
        for cidr in cidrs.split(",")
        if cidr.strip()
    )


def _trusted(address: str) -> bool:
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(addr in network for network in _networks(settings.trusted_proxy_cidrs))


def resolve_client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if not peer or not _trusted(peer):
        return peer or "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    for raw in reversed(
        [part.strip() for part in forwarded.split(",") if part.strip()]
    ):
        if not _trusted(raw):
            return raw
    return peer
