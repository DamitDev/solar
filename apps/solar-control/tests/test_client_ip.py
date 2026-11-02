"""Tests for the trusted-proxy client IP resolution."""

from starlette.requests import Request

from app.services.client_ip import resolve_client_ip


def _request(peer: str, forwarded: str | None = None) -> Request:
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request({"type": "http", "client": (peer, 1234), "headers": headers})


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
    assert resolve_client_ip(_request("")) == "unknown"
