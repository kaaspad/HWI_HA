"""Support for Lutron Homeworks covers."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HomeworksData, HomeworksEntity
from .const import (
    CONF_ADDR,
    CONF_CONTROLLER_ID,
    CONF_COVERS,
    DEFAULT_COVER_NAME,
    DOMAIN,
)
from .pyhomeworks.pyhomeworks import HW_COVER_STATE_CHANGED, Homeworks

_LOGGER = logging.getLogger(__name__)

# Cover position constants
COVER_UP = 16
COVER_DOWN = 35
COVER_STOP = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Homeworks covers."""
    data: HomeworksData = hass.data[DOMAIN][entry.entry_id]
    controller = data.controller
    controller_id = entry.options[CONF_CONTROLLER_ID]
    entities = []
    for cover in entry.options.get(CONF_COVERS, []):
        entity = HomeworksCover(
            controller,
            controller_id,
            cover[CONF_ADDR],
            cover[CONF_NAME],
        )
        entities.append(entity)
    async_add_entities(entities, True)


class HomeworksCover(HomeworksEntity, CoverEntity):
    """Homeworks Cover."""

    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        controller: Homeworks,
        controller_id: str,
        addr: str,
        name: str,
    ) -> None:
        """Create device with Addr and name."""
        super().__init__(controller, controller_id, addr, 0, None)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{controller_id}.{addr}")}, name=name
        )
        self._current_position = None
        self._is_closed = None

    async def async_added_to_hass(self) -> None:
        """Call when entity is added to hass."""
        signal = f"homeworks_entity_{self._controller_id}_{self._addr}"
        _LOGGER.debug("connecting %s", signal)
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal, self._update_callback)
        )
        self._controller.request_cover_state(self._addr)
        
    def open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        self._controller.fade_dim(16, 0, 0, self._addr)

    def close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        self._controller.fade_dim(35, 0, 0, self._addr)

    def stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        self._controller.fade_dim(0, 0, 0, self._addr)

    @property
    def is_closed(self) -> bool | None:
        """Return true if the cover is closed."""
        return self._is_closed

    @property
    def current_cover_position(self) -> int | None:
        """Return the current position of the cover."""
        return self._current_position 

    @callback
    def _update_callback(self, msg_type: str, values: list[Any]) -> None:
        """Process device specific messages."""
        
        if msg_type == HW_COVER_STATE_CHANGED:
            state = int(values[0])
            if state == COVER_UP:
                self._is_closed = False
                self._current_position = 100
            elif state == COVER_DOWN:
                self._is_closed = True
                self._current_position = 0
            elif state == COVER_STOP:
                # Keep current position
                pass
            self.async_write_ha_state()

