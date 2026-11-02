"""Resolve the real client IP behind the ingress.

The ingress forwards the real client address in X-Forwarded-For and trusts only
the front proxy, so the header is trustworthy only when the immediate peer is an
ingress pod. A direct pod-to-pod caller keeps its own address, and a header from
an untrusted peer is ignored. The rightmost untrusted hop is the client: trusted
proxies are stripped from the right, exactly as nginx's real_ip_recursive does.
"""

import ipaddress
import logging
from functools import lru_cache

from fastapi import Request

from app.config import settings

logger = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
_LOOPBACK = ("127.0.0.1", "::1")


@lru_cache(maxsize=8)
def _networks(cidrs: str) -> tuple[Network, ...]:
    networks: list[Network] = []
    for cidr in cidrs.split(","):
        raw = cidr.strip()
        if not raw:
            continue
        try:
            networks.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            # A typo in the setting must not turn every /v1 request into a 500;
            # the worst case is that no proxy is trusted.
            logger.warning("Ignoring invalid trusted proxy CIDR: %s", raw)
    return tuple(networks)


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
    if peer in _LOOPBACK:
        # The cursor proxy's self-call arrives over loopback with the caller's
        # address in a dedicated header; X-Forwarded-For would be rewritten
        # into the client scope by uvicorn's proxy-headers middleware.
        dedicated = request.headers.get("x-solar-client-ip", "")
        if dedicated:
            try:
                ipaddress.ip_address(dedicated)
            except ValueError:
                return peer
            return dedicated
    forwarded = request.headers.get("x-forwarded-for", "")
    for raw in reversed(
        [part.strip() for part in forwarded.split(",") if part.strip()]
    ):
        if not _trusted(raw):
            try:
                ipaddress.ip_address(raw)
            except ValueError:
                # A trusted peer can put anything in the header; never record
                # an unparseable value as the client address.
                return peer
            return raw
    return peer
