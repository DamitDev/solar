"""Tests for the cursor proxy's self-call headers."""

from app.cursor_proxy.service import _upstream_headers


def test_upstream_headers_carry_the_client_ip_and_internal_marker():
    headers = _upstream_headers("Bearer sk-test", False, client_ip="203.0.113.5")
    assert headers["X-Solar-Client-IP"] == "203.0.113.5"
    assert "X-Forwarded-For" not in headers
    assert headers["X-Solar-Internal"] == "1"


def test_upstream_headers_without_a_client_ip_omit_the_forwarded_header():
    headers = _upstream_headers("Bearer sk-test", False)
    assert "X-Solar-Client-IP" not in headers
    assert "X-Forwarded-For" not in headers
    assert headers["X-Solar-Internal"] == "1"
