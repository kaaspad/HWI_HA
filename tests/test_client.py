"""Tests for the Homeworks async client.

These tests require Home Assistant and are skipped when HA is not installed.
"""

import pytest

# Mark entire module as requiring HA
pytestmark = pytest.mark.requires_ha


@pytest.fixture
async def fake_controller():
    """Create and start a fake controller."""
    from .fake_controller import FakeHomeworksController
    controller = FakeHomeworksController(port=0)
    await controller.start()
    yield controller
    await controller.stop()


# These tests are placeholders - they require HA for the full client
class TestHomeworksClientIntegration:
    """Integration tests for HomeworksClient (requires HA)."""

    @pytest.mark.asyncio
    async def test_placeholder(self):
        """Placeholder test - actual tests require HA."""
        pytest.skip("Full client tests require Home Assistant")
