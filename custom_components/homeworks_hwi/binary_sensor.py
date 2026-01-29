"""Support for Lutron Homeworks binary sensors (keypad LED indicators)."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HomeworksData
from .const import (
    CONF_ADDR,
    CONF_BUTTONS,
    CONF_CONTROLLER_ID,
    CONF_KEYPADS,
    CONF_LED,
    CONF_NAME,
    CONF_NUMBER,
    DOMAIN,
)
from .coordinator import HomeworksCoordinator
from .models import normalize_address

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Homeworks binary sensors."""
    data: HomeworksData = hass.data[DOMAIN][entry.entry_id]
    coordinator = data.coordinator
    controller_id = entry.options[CONF_CONTROLLER_ID]
    entities: list[HomeworksBinarySensor] = []

    for keypad in entry.options.get(CONF_KEYPADS, []):
        keypad_addr = normalize_address(keypad[CONF_ADDR])
        keypad_name = keypad.get(CONF_NAME, "Keypad")

        # Register keypad address for KLS polling
        coordinator._kls_poll_addresses.add(keypad_addr)

        for button in keypad.get(CONF_BUTTONS, []):
            if not button.get(CONF_LED, False):
                continue

            entity = HomeworksBinarySensor(
                coordinator=coordinator,
                controller_id=controller_id,
                keypad_addr=keypad_addr,
                keypad_name=keypad_name,
                button_name=button.get(CONF_NAME, "Button"),
                led_number=button[CONF_NUMBER],
            )
            entities.append(entity)

    if entities:
        _LOGGER.debug("Adding %d binary sensor entities", len(entities))
        async_add_entities(entities)


class HomeworksBinarySensor(
    CoordinatorEntity[HomeworksCoordinator], BinarySensorEntity
):
    """Homeworks Binary Sensor for keypad LED state."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HomeworksCoordinator,
        controller_id: str,
        keypad_addr: str,
        keypad_name: str,
        button_name: str,
        led_number: int,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._controller_id = controller_id
        self._keypad_addr = keypad_addr
        self._led_number = led_number

        self._attr_unique_id = (
            f"homeworks.{controller_id}.led.{keypad_addr}.{led_number}"
        )
        self._attr_name = button_name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{controller_id}.{keypad_addr}")},
            name=keypad_name,
            manufacturer="Lutron",
            model="HomeWorks Keypad",
        )
        self._attr_extra_state_attributes = {
            "homeworks_address": keypad_addr,
            "led_number": led_number,
        }

    @property
    def is_on(self) -> bool | None:
        """Return True if the LED is on.

        LED states: 0=Off, 1=On, 2=Flash1, 3=Flash2
        We treat Flash as On.
        """
        led_states = self.coordinator.get_keypad_led_states(self._keypad_addr)
        if not led_states or self._led_number > len(led_states):
            return None

        # LED number is 1-indexed
        led_value = led_states[self._led_number - 1]
        return led_value > 0  # 1, 2, or 3 are all "on"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register for updates when added to hass."""
        await super().async_added_to_hass()

        # Request initial state
        await self.coordinator.async_request_keypad_led_states(self._keypad_addr)
