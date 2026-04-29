"""Compatibility router module.

The ``/api/models``, ``/api/datasets``, and ``/api/artifacts`` endpoints are
defined in their dedicated route modules. This module intentionally exposes an
empty router to avoid duplicate route definitions and path conflicts.
"""

from fastapi import APIRouter

router = APIRouter()
