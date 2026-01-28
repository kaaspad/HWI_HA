"""Pytest configuration for Homeworks tests."""

import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "requires_ha: mark test as requiring Home Assistant"
    )
