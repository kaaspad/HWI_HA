"""DataUpdateCoordinator for Lutron Homeworks integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .client import (
    HW_BUTTON_DOUBLE_TAP,
    HW_BUTTON_HOLD,
    HW_BUTTON_PRESSED,
    HW_BUTTON_RELEASED,
    HW_CONNECTION_LOST,
    HW_CONNECTION_RESTORED,
    HW_KEYPAD_LED_CHANGED,
    HW_LIGHT_CHANGED,
    HomeworksClient,
    HomeworksClientConfig,
)
from .models import (
    CCO_BUTTON_WINDOW_OFFSET,
    CCOAddress,
    CCODevice,
    ControllerHealth,
    normalize_address,
)

_LOGGER = logging.getLogger(__name__)

# Default polling interval for KLS (CCO state)
DEFAULT_KLS_POLL_INTERVAL = timedelta(seconds=10)

# Default polling interval for dimmer state
DEFAULT_DIMMER_POLL_INTERVAL = timedelta(seconds=30)


class HomeworksCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Homeworks data updates.

    This coordinator:
    - Manages the async client connection
    - Polls KLS state for CCO devices at regular intervals
    - Maintains a unified state cache for all CCO entities
    - Dispatches state updates to entities
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config: HomeworksClientConfig,
        controller_id: str,
        kls_poll_interval: timedelta = DEFAULT_KLS_POLL_INTERVAL,
        kls_window_offset: int = CCO_BUTTON_WINDOW_OFFSET,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Homeworks {controller_id}",
            update_interval=kls_poll_interval,
        )
        self._config = config
        self._controller_id = controller_id
        self._client: HomeworksClient | None = None
        self._kls_window_offset = kls_window_offset

        # CCO device registry: unique_key -> CCODevice
        self._cco_devices: dict[tuple[int, int, int, int], CCODevice] = {}

        # CCO state cache: unique_key -> bool (is_on)
        self._cco_states: dict[tuple[int, int, int, int], bool] = {}

        # Dimmer state cache: address -> level (0-100)
        self._dimmer_states: dict[str, int] = {}

        # Keypad LED state cache: address -> list[int]
        self._keypad_led_states: dict[str, list[int]] = {}

        # Addresses that need KLS polling
        self._kls_poll_addresses: set[str] = set()

        # Dimmer addresses for polling
        self._dimmer_addresses: set[str] = set()

        # Event callbacks
        self._button_callbacks: dict[str, list[callable[[str, int, str], None]]] = {}

    @property
    def controller_id(self) -> str:
        """Return the controller ID."""
        return self._controller_id

    @property
    def client(self) -> HomeworksClient | None:
        """Return the client instance."""
        return self._client

    @property
    def health(self) -> ControllerHealth:
        """Return controller health metrics."""
        if self._client:
            return self._client.health
        return ControllerHealth()

    @property
    def connected(self) -> bool:
        """Return True if connected to controller."""
        return self._client is not None and self._client.connected

    def register_cco_device(self, device: CCODevice) -> None:
        """Register a CCO device for state tracking."""
        key = device.address.unique_key
        self._cco_devices[key] = device
        self._cco_states[key] = False  # Default to off

        # Register the KLS address for polling
        kls_addr = device.address.to_kls_address()
        self._kls_poll_addresses.add(kls_addr)
        if self._client:
            self._client.register_kls_address(kls_addr)

        _LOGGER.debug(
            "Registered CCO device: %s (type=%s, inverted=%s)",
            device.address,
            device.entity_type.name,
            device.inverted,
        )

    def unregister_cco_device(self, address: CCOAddress) -> None:
        """Unregister a CCO device."""
        key = address.unique_key
        self._cco_devices.pop(key, None)
        self._cco_states.pop(key, None)

    def register_dimmer(self, address: str) -> None:
        """Register a dimmer for state tracking."""
        normalized = normalize_address(address)
        self._dimmer_addresses.add(normalized)
        self._dimmer_states[normalized] = 0

    def unregister_dimmer(self, address: str) -> None:
        """Unregister a dimmer."""
        normalized = normalize_address(address)
        self._dimmer_addresses.discard(normalized)
        self._dimmer_states.pop(normalized, None)

    def get_cco_state(self, address: CCOAddress) -> bool:
        """Get the current state of a CCO device."""
        return self._cco_states.get(address.unique_key, False)

    def get_dimmer_level(self, address: str) -> int:
        """Get the current dimmer level."""
        normalized = normalize_address(address)
        return self._dimmer_states.get(normalized, 0)

    def get_keypad_led_states(self, address: str) -> list[int]:
        """Get LED states for a keypad."""
        normalized = normalize_address(address)
        return self._keypad_led_states.get(normalized, [0] * 24)

    def register_button_callback(
        self,
        address: str,
        callback: callable[[str, int, str], None],
    ) -> callable[[], None]:
        """Register a callback for button events.

        Returns a function to unregister the callback.
        """
        normalized = normalize_address(address)
        if normalized not in self._button_callbacks:
            self._button_callbacks[normalized] = []
        self._button_callbacks[normalized].append(callback)

        def unregister():
            self._button_callbacks[normalized].remove(callback)
            if not self._button_callbacks[normalized]:
                del self._button_callbacks[normalized]

        return unregister

    async def async_setup(self) -> bool:
        """Set up the coordinator and connect to the controller."""
        self._client = HomeworksClient(
            config=self._config,
            message_callback=self._handle_message,
        )

        # Register existing KLS addresses
        for addr in self._kls_poll_addresses:
            self._client.register_kls_address(addr)

        # Connect
        if not await self._client.connect():
            return False

        # Start the read loop
        await self._client.start()

        # Initial poll
        await self._poll_all_states()

        return True

    async def async_shutdown(self) -> None:
        """Shut down the coordinator."""
        if self._client:
            await self._client.stop()
            self._client = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from controller (called by DataUpdateCoordinator)."""
        if not self._client or not self._client.connected:
            raise UpdateFailed("Not connected to controller")

        # Poll all KLS addresses
        await self._poll_kls_states()

        # Return current state
        return {
            "cco_states": dict(self._cco_states),
            "dimmer_states": dict(self._dimmer_states),
            "connected": self.connected,
            "last_update": datetime.now().isoformat(),
        }

    async def _poll_all_states(self) -> None:
        """Poll all device states."""
        await self._poll_kls_states()
        await self._poll_dimmer_states()

    async def _poll_kls_states(self) -> None:
        """Poll KLS state for all registered addresses."""
        if not self._client:
            return

        for address in list(self._kls_poll_addresses):
            try:
                await self._client.request_keypad_led_states(address)
                # Small delay between requests
                await asyncio.sleep(0.1)
            except Exception as err:
                _LOGGER.warning("Failed to poll KLS for %s: %s", address, err)

    async def _poll_dimmer_states(self) -> None:
        """Poll dimmer levels for all registered dimmers."""
        if not self._client:
            return

        for address in list(self._dimmer_addresses):
            try:
                await self._client.request_dimmer_level(address)
                await asyncio.sleep(0.05)
            except Exception as err:
                _LOGGER.warning("Failed to poll dimmer %s: %s", address, err)

    @callback
    def _handle_message(self, msg_type: str, values: list[Any]) -> None:
        """Handle incoming messages from the controller."""
        if msg_type == HW_KEYPAD_LED_CHANGED:
            self._handle_kls_update(values[0], values[1])
        elif msg_type == HW_LIGHT_CHANGED:
            self._handle_dimmer_update(values[0], values[1])
        elif msg_type == HW_BUTTON_PRESSED:
            self._dispatch_button_event(values[0], values[1], "pressed")
        elif msg_type == HW_BUTTON_RELEASED:
            self._dispatch_button_event(values[0], values[1], "released")
        elif msg_type == HW_BUTTON_HOLD:
            self._dispatch_button_event(values[0], values[1], "hold")
        elif msg_type == HW_BUTTON_DOUBLE_TAP:
            self._dispatch_button_event(values[0], values[1], "double_tap")
        elif msg_type == HW_CONNECTION_LOST:
            _LOGGER.warning("Controller connection lost")
        elif msg_type == HW_CONNECTION_RESTORED:
            _LOGGER.info("Controller connection restored")
            # Re-poll all states after reconnection
            self.hass.async_create_task(self._poll_all_states())

    def _handle_kls_update(self, address: str, led_states: list[int]) -> None:
        """Handle a KLS (LED state) update.

        This is the core of the CCO state engine - it updates all CCO
        devices that match this address.
        """
        normalized = normalize_address(address)
        self._keypad_led_states[normalized] = led_states

        _LOGGER.debug("KLS update for %s: %s", normalized, led_states)

        # Parse the address to get processor/link/address
        try:
            parts = normalized.strip("[]").split(":")
            processor = int(parts[0])
            link = int(parts[1])
            addr = int(parts[2])
        except (ValueError, IndexError):
            _LOGGER.warning("Failed to parse KLS address: %s", normalized)
            return

        # Update all CCO devices at this address
        state_changed = False
        for key, device in self._cco_devices.items():
            if (
                device.address.processor == processor
                and device.address.link == link
                and device.address.address == addr
            ):
                # Get the button state from the button window
                # The 8 CCO buttons are at indices window_offset to window_offset+7
                # Button N (1-8) is at index window_offset + (N-1)
                button = device.address.button
                if 1 <= button <= 8:
                    index = self._kls_window_offset + (button - 1)
                    if index < len(led_states):
                        led_value = led_states[index]
                        new_state = device.interpret_state(led_value)
                        old_state = self._cco_states.get(key)

                        if old_state != new_state:
                            self._cco_states[key] = new_state
                            state_changed = True
                            _LOGGER.debug(
                                "CCO %s state changed: %s -> %s (LED=%d)",
                                device.address,
                                old_state,
                                new_state,
                                led_value,
                            )

        # Notify listeners if any state changed
        if state_changed:
            self.async_set_updated_data(
                {
                    "cco_states": dict(self._cco_states),
                    "dimmer_states": dict(self._dimmer_states),
                    "connected": self.connected,
                    "last_update": datetime.now().isoformat(),
                }
            )

    def _handle_dimmer_update(self, address: str, level: int) -> None:
        """Handle a dimmer level update."""
        normalized = normalize_address(address)

        if normalized in self._dimmer_states:
            old_level = self._dimmer_states[normalized]
            if old_level != level:
                self._dimmer_states[normalized] = level
                _LOGGER.debug(
                    "Dimmer %s level changed: %d -> %d",
                    normalized,
                    old_level,
                    level,
                )
                self.async_set_updated_data(
                    {
                        "cco_states": dict(self._cco_states),
                        "dimmer_states": dict(self._dimmer_states),
                        "connected": self.connected,
                        "last_update": datetime.now().isoformat(),
                    }
                )

    def _dispatch_button_event(
        self, address: str, button: int, event_type: str
    ) -> None:
        """Dispatch button event to registered callbacks."""
        normalized = normalize_address(address)
        callbacks = self._button_callbacks.get(normalized, [])

        for cb in callbacks:
            try:
                cb(normalized, button, event_type)
            except Exception as err:
                _LOGGER.error("Button callback error: %s", err)

    # === Command Methods (proxies to client) ===

    async def async_cco_close(self, address: CCOAddress) -> bool:
        """Close a CCO relay (turn on)."""
        if not self._client:
            return False
        return await self._client.cco_close(
            address.to_command_address(), address.button
        )

    async def async_cco_open(self, address: CCOAddress) -> bool:
        """Open a CCO relay (turn off)."""
        if not self._client:
            return False
        return await self._client.cco_open(address.to_command_address(), address.button)

    async def async_fade_dim(
        self,
        address: str,
        level: float,
        fade_time: float = 1.0,
        delay_time: float = 0.0,
    ) -> bool:
        """Fade a dimmer to a level."""
        if not self._client:
            return False
        normalized = normalize_address(address)
        return await self._client.fade_dim(level, fade_time, delay_time, normalized)

    async def async_request_dimmer_level(self, address: str) -> bool:
        """Request current dimmer level."""
        if not self._client:
            return False
        normalized = normalize_address(address)
        return await self._client.request_dimmer_level(normalized)

    async def async_keypad_button_press(self, address: str, button: int) -> bool:
        """Simulate a keypad button press."""
        if not self._client:
            return False
        normalized = normalize_address(address)
        return await self._client.keypad_button_press(normalized, button)

    async def async_keypad_button_release(self, address: str, button: int) -> bool:
        """Simulate a keypad button release."""
        if not self._client:
            return False
        normalized = normalize_address(address)
        return await self._client.keypad_button_release(normalized, button)

    async def async_request_keypad_led_states(self, address: str) -> bool:
        """Request keypad LED states."""
        if not self._client:
            return False
        normalized = normalize_address(address)
        return await self._client.request_keypad_led_states(normalized)
