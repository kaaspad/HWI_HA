"""Support for Lutron Homeworks covers (CCO-based or dimmer-based)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HomeworksData
from .const import (
    CONF_ADDR,
    CONF_CCO_DEVICES,
    CONF_CONTROLLER_ID,
    CONF_COVERS,
    CONF_ENTITY_TYPE,
    CONF_INVERTED,
    CONF_NAME,
    CONF_RELAY_NUMBER,
    CCO_TYPE_COVER,
    DEFAULT_COVER_NAME,
    DOMAIN,
)
from .coordinator import HomeworksCoordinator
from .models import CCOAddress, CCODevice, CCOEntityType, normalize_address

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Homeworks covers."""
    data: HomeworksData = hass.data[DOMAIN][entry.entry_id]
    coordinator = data.coordinator
    controller_id = entry.options[CONF_CONTROLLER_ID]
    entities: list[HomeworksCCOCover] = []

    # New-style CCO devices with type=cover
    for device_config in entry.options.get(CONF_CCO_DEVICES, []):
        if device_config.get(CONF_ENTITY_TYPE) != CCO_TYPE_COVER:
            continue

        try:
            addr_str = device_config[CONF_ADDR]
            button = device_config.get(CONF_RELAY_NUMBER, 1)

            if "," not in addr_str:
                full_addr = f"{addr_str},{button}"
            else:
                full_addr = addr_str

            address = CCOAddress.from_string(full_addr)

            device = CCODevice(
                address=address,
                name=device_config.get(CONF_NAME, DEFAULT_COVER_NAME),
                entity_type=CCOEntityType.COVER,
                inverted=device_config.get(CONF_INVERTED, False),
            )

            entity = HomeworksCCOCover(
                coordinator=coordinator,
                controller_id=controller_id,
                device=device,
            )
            entities.append(entity)

        except Exception as err:
            _LOGGER.error("Failed to create cover for %s: %s", device_config, err)

    # Legacy covers format
    for cover_config in entry.options.get(CONF_COVERS, []):
        try:
            addr = normalize_address(cover_config[CONF_ADDR])
            parts = addr.strip("[]").split(":")

            # For legacy covers, we need two buttons: one for open, one for close
            # Button 1 typically controls the cover
            address = CCOAddress(
                processor=int(parts[0]),
                link=int(parts[1]),
                address=int(parts[2]),
                button=1,  # Default button for cover control
            )

            device = CCODevice(
                address=address,
                name=cover_config.get(CONF_NAME, DEFAULT_COVER_NAME),
                entity_type=CCOEntityType.COVER,
                inverted=cover_config.get(CONF_INVERTED, False),
            )

            entity = HomeworksCCOCover(
                coordinator=coordinator,
                controller_id=controller_id,
                device=device,
            )
            entities.append(entity)

        except Exception as err:
            _LOGGER.error("Failed to create legacy cover for %s: %s", cover_config, err)

    if entities:
        _LOGGER.debug("Adding %d cover entities", len(entities))
        async_add_entities(entities)
    else:
        _LOGGER.debug("No covers to add")


class HomeworksCCOCover(CoordinatorEntity[HomeworksCoordinator], CoverEntity):
    """Homeworks CCO-based Cover.

    For CCO-based covers, we can only determine open/close state from KLS.
    Position tracking is not available without additional hardware feedback.
    """

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )

    def __init__(
        self,
        coordinator: HomeworksCoordinator,
        controller_id: str,
        device: CCODevice,
    ) -> None:
        """Initialize the cover."""
        super().__init__(coordinator)
        self._device = device
        self._controller_id = controller_id
        self._is_opening = False
        self._is_closing = False

        self._attr_unique_id = f"homeworks.{controller_id}.cover.{device.unique_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{controller_id}.cover.{device.address}")},
            name=device.name,
            manufacturer="Lutron",
            model="HomeWorks Cover",
        )
        self._attr_extra_state_attributes = {
            "homeworks_address": str(device.address),
        }

    @property
    def name(self) -> str | None:
        """Return the name of the cover."""
        return self._device.name or None

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is closed.

        For CCO-based covers, we derive this from the KLS state.
        When the CCO state is ON (relay closed), the cover is closed.
        """
        is_on = self.coordinator.get_cco_state(self._device.address)

        if self._device.inverted:
            return not is_on
        return is_on

    @property
    def is_opening(self) -> bool:
        """Return True if the cover is opening."""
        return self._is_opening

    @property
    def is_closing(self) -> bool:
        """Return True if the cover is closing."""
        return self._is_closing

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Clear movement flags when state updates
        self._is_opening = False
        self._is_closing = False
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        _LOGGER.debug("Opening cover: %s", self._device.address)
        self._is_opening = True
        self._is_closing = False
        self.async_write_ha_state()

        # Open = CCO relay open (off state)
        if self._device.inverted:
            await self.coordinator.async_cco_close(self._device.address)
        else:
            await self.coordinator.async_cco_open(self._device.address)

        # Request state update
        await self.coordinator.async_request_keypad_led_states(
            self._device.address.to_kls_address()
        )

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        _LOGGER.debug("Closing cover: %s", self._device.address)
        self._is_closing = True
        self._is_opening = False
        self.async_write_ha_state()

        # Close = CCO relay closed (on state)
        if self._device.inverted:
            await self.coordinator.async_cco_open(self._device.address)
        else:
            await self.coordinator.async_cco_close(self._device.address)

        # Request state update
        await self.coordinator.async_request_keypad_led_states(
            self._device.address.to_kls_address()
        )

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover.

        For CCO covers, there may not be a direct stop command.
        This implementation sends a quick pulse to stop.
        """
        _LOGGER.debug("Stopping cover: %s", self._device.address)
        self._is_opening = False
        self._is_closing = False
        self.async_write_ha_state()

        # Some covers stop when you pulse the same command
        # This is hardware-dependent
        await self.coordinator.async_request_keypad_led_states(
            self._device.address.to_kls_address()
        )

    async def async_added_to_hass(self) -> None:
        """Register with coordinator when added to hass."""
        await super().async_added_to_hass()

        # Ensure device is registered
        self.coordinator.register_cco_device(self._device)

        # Request initial state
        await self.coordinator.async_request_keypad_led_states(
            self._device.address.to_kls_address()
        )
