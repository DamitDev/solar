"""Tests for the trusted-proxy client IP resolution."""

from starlette.requests import Request

from app.services.client_ip import resolve_client_ip


def _request(
    peer: str | None,
    forwarded: str | None = None,
    dedicated: str | None = None,
) -> Request:
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    if dedicated is not None:
        headers.append((b"x-solar-client-ip", dedicated.encode()))
    scope = {"type": "http", "headers": headers}
    if peer is not None:
        scope["client"] = (peer, 1234)
    return Request(scope)


def test_trusted_peer_uses_the_forwarded_address():
    assert resolve_client_ip(_request("10.42.1.2", "203.0.113.5")) == "203.0.113.5"


def test_untrusted_peer_ignores_the_header():
    assert resolve_client_ip(_request("198.51.100.9", "203.0.113.5")) == "198.51.100.9"


def test_rightmost_untrusted_hop_wins():
    request = _request("10.42.1.2", "203.0.113.5, 10.42.9.9")
    assert resolve_client_ip(request) == "203.0.113.5"


def test_missing_header_returns_the_peer():
    assert resolve_client_ip(_request("10.42.1.2")) == "10.42.1.2"


def test_missing_peer_returns_unknown():
    assert resolve_client_ip(_request(None)) == "unknown"


def test_ipv6_forwarded_address_is_used():
    assert resolve_client_ip(_request("10.42.1.2", "2001:db8::5")) == "2001:db8::5"


def test_malformed_forwarded_address_falls_back_to_the_peer():
    request = _request("10.42.1.2", "not-an-ip")
    assert resolve_client_ip(request) == "10.42.1.2"


def test_loopback_peer_uses_the_forwarded_address():
    assert resolve_client_ip(_request("127.0.0.1", "203.0.113.5")) == "203.0.113.5"


def test_loopback_peer_reads_the_dedicated_header():
    request = _request("127.0.0.1", dedicated="203.0.113.5")
    assert resolve_client_ip(request) == "203.0.113.5"


def test_loopback_peer_ignores_an_invalid_dedicated_header():
    request = _request("127.0.0.1", dedicated="not-an-ip")
    assert resolve_client_ip(request) == "127.0.0.1"


def test_non_loopback_peer_ignores_the_dedicated_header():
    request = _request("10.42.1.2", dedicated="203.0.113.5")
    assert resolve_client_ip(request) == "10.42.1.2"
