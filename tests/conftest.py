"""Pytest configuration for Homeworks tests."""

import sys
from pathlib import Path

import pytest

# Add custom_components/homeworks to path so tests can import models directly
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root / "custom_components" / "homeworks"))


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "requires_ha: mark test as requiring Home Assistant"
    )
