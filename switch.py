"""Support for Lutron Homeworks CCO relays as switches."""

from __future__ import annotations

import logging
from typing import Any
from datetime import timedelta

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HomeworksData, HomeworksEntity
from .const import (
    CONF_ADDR,
    CONF_CONTROLLER_ID,
    CONF_CCOS,
    CONF_RELAY_NUMBER,
    DEFAULT_CCO_NAME,
    DOMAIN,
    CONF_LED_ADDR,
    CONF_LED_NUMBER,
)
from .pyhomeworks.pyhomeworks import HW_CCO_STATE_CHANGED, Homeworks, HW_KEYPAD_LED_CHANGED

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=10)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Homeworks CCO relays as switches."""
    data: HomeworksData = hass.data[DOMAIN][entry.entry_id]
    controller = data.controller
    controller_id = entry.options[CONF_CONTROLLER_ID]
    entities = []
    
    _LOGGER.debug("Setting up CCO switches. Config: %s", entry.options.get(CONF_CCOS, []))
    
    for cco in entry.options.get(CONF_CCOS, []):
        _LOGGER.debug("Creating CCO switch with config: %s", cco)
        # Default led_addr and led_number to the CCO's own address and relay number if not set
        led_addr = cco.get(CONF_LED_ADDR, cco[CONF_ADDR])
        led_number = cco.get(CONF_LED_NUMBER, cco[CONF_RELAY_NUMBER])
        entity = HomeworksCCOSwitch(
            hass,
            controller,
            controller_id,
            cco[CONF_ADDR],
            cco.get(CONF_NAME, DEFAULT_CCO_NAME),
            cco[CONF_RELAY_NUMBER],
            led_addr,
            led_number,
        )
        entities.append(entity)
    
    if entities:
        _LOGGER.debug("Adding %s CCO switch entities", len(entities))
        async_add_entities(entities, True)
    else:
        _LOGGER.debug("No CCO switches to add")


class HomeworksCCOSwitch(HomeworksEntity, SwitchEntity):
    """Homeworks CCO Relay Switch."""

    _attr_has_entity_name = True
    _attr_should_poll = True  # Enable polling

    def __init__(
        self,
        hass: HomeAssistant,
        controller: Homeworks,
        controller_id: str,
        addr: str,
        name: str,
        relay_number: int,
        led_addr: str = None,
        led_number: int = None,
    ) -> None:
        """Create device with Addr, name, and relay number."""
        super().__init__(controller, controller_id, addr, relay_number, None)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{controller_id}.{addr}.{relay_number}")},
            name=name,
            manufacturer="Lutron",
            model="CCO Relay",
        )
        self._relay_number = relay_number
        self._is_on = False
        self._hass = hass
        self._led_addr = led_addr
        self._led_number = led_number

    async def async_added_to_hass(self) -> None:
        """Call when entity is added to hass."""
        if self._led_addr:
            signal_led = f"homeworks_entity_{self._controller_id}_{self._led_addr}"
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal_led, self._update_callback)
            )
        # No need to request state since we're using LED monitoring

    @callback
    def _update_callback(self, msg_type: str, values: list[Any]) -> None:
        """Process device specific messages."""
        _LOGGER.debug(
            "CCO update callback: msg_type=%s, values=%s, relay=%s, addr=%s",
            msg_type, values, self._relay_number, self._addr
        )
        
        if (
            msg_type == HW_KEYPAD_LED_CHANGED
            and values[0] == self._led_addr  # Match LED address
            and self._led_number is not None
        ):
            # For CCOs, check position 9-16 (0-based) for outputs 1-8
            led_states = values[1]
            if len(led_states) >= 17:  # Make sure we have enough LED states
                # Convert relay number (1-8) to LED position (9-16)
                led_position = self._led_number + 8  # This maps 1->9, 2->10, etc.
                led_state = led_states[led_position]
                # 1 means device is OFF, 2 means device is ON
                self._is_on = led_state == 2
                self.async_write_ha_state()

    def turn_on(self, **kwargs: Any) -> None:
        """Close the CCO relay."""
        self._controller.cco_close(self._addr, self._relay_number)
        # State will be updated via LED monitoring

    def turn_off(self, **kwargs: Any) -> None:
        """Open the CCO relay."""
        self._controller.cco_open(self._addr, self._relay_number)
        # State will be updated via LED monitoring

    @property
    def is_on(self) -> bool:
        """Return true if the CCO relay is closed."""
        return self._is_on

    async def async_update(self) -> None:
        """Poll the current state from the controller."""
        # No need to poll since we're using LED monitoring
        pass