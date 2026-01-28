"""Support for Lutron Homeworks Series 4 and 8 systems.

HA 2026.1 compliant:
- Credentials (host, port, username, password) in entry.data
- Non-secrets (devices, settings) in entry.options
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify

from .client import HomeworksClient, HomeworksClientConfig
from .const import (
    CONF_ADDR,
    CONF_BUTTON_NUMBER,
    CONF_CCO_DEVICES,
    CONF_CCOS,
    CONF_CONTROLLER_ID,
    CONF_COVERS,
    CONF_DIMMERS,
    CONF_ENTITY_TYPE,
    CONF_INVERTED,
    CONF_KEYPADS,
    CONF_KLS_POLL_INTERVAL,
    CONF_KLS_WINDOW_OFFSET,
    CONF_LOCKS,
    CONF_RATE,
    CONF_RELAY_NUMBER,
    CCO_TYPE_COVER,
    CCO_TYPE_LIGHT,
    CCO_TYPE_LOCK,
    CCO_TYPE_SWITCH,
    DEFAULT_FADE_RATE,
    DEFAULT_KLS_POLL_INTERVAL,
    DEFAULT_KLS_WINDOW_OFFSET,
    DOMAIN,
)
from .coordinator import HomeworksCoordinator
from .models import CCOAddress, CCODevice, CCOEntityType, ControllerHealth, normalize_address

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.COVER,
    Platform.LIGHT,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONF_COMMAND = "command"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SEND_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONTROLLER_ID): str,
        vol.Required(CONF_COMMAND): vol.All(cv.ensure_list, [str]),
    }
)


@dataclass
class HomeworksData:
    """Container for config entry data."""

    coordinator: HomeworksCoordinator
    controller_id: str


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for Lutron Homeworks."""

    async def async_call_service(service_call: ServiceCall) -> None:
        """Call the service."""
        await async_send_command(hass, service_call.data)

    hass.services.async_register(
        DOMAIN,
        "send_command",
        async_call_service,
        schema=SERVICE_SEND_COMMAND_SCHEMA,
    )


async def async_send_command(hass: HomeAssistant, data: Mapping[str, Any]) -> None:
    """Send command to a controller."""

    def get_controller_ids() -> list[str]:
        """Get controller IDs."""
        return [hw_data.controller_id for hw_data in hass.data[DOMAIN].values()]

    def get_homeworks_data(controller_id: str) -> HomeworksData | None:
        """Get homeworks data for controller ID."""
        for hw_data in hass.data[DOMAIN].values():
            if hw_data.controller_id == controller_id:
                return hw_data
        return None

    homeworks_data = get_homeworks_data(data[CONF_CONTROLLER_ID])
    if not homeworks_data:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_controller_id",
            translation_placeholders={
                "controller_id": data[CONF_CONTROLLER_ID],
                "controller_ids": ",".join(get_controller_ids()),
            },
        )

    commands = data[CONF_COMMAND]
    _LOGGER.debug("Send commands: %s", commands)

    client = homeworks_data.coordinator.client
    if not client:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_connected",
        )

    for command in commands:
        if command.lower().startswith("delay"):
            delay = int(command.partition(" ")[2])
            _LOGGER.debug("Sleeping for %s ms", delay)
            await asyncio.sleep(delay / 1000)
        else:
            _LOGGER.debug("Sending command '%s'", command)
            await client.send_command(command)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Start Homeworks controller."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Homeworks from a config entry.

    Credentials are read from entry.data (secrets).
    Devices and settings are read from entry.options (non-secrets).
    """
    hass.data.setdefault(DOMAIN, {})

    # Read credentials from entry.data
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)

    # Read non-secrets from entry.options
    options = entry.options
    controller_id = options.get(CONF_CONTROLLER_ID, slugify(entry.title))

    # Build client config
    client_config = HomeworksClientConfig(
        host=host,
        port=port,
        username=username,
        password=password,
    )

    # Get poll interval and window offset from options
    kls_poll_interval = options.get(CONF_KLS_POLL_INTERVAL, DEFAULT_KLS_POLL_INTERVAL)
    kls_window_offset = options.get(CONF_KLS_WINDOW_OFFSET, DEFAULT_KLS_WINDOW_OFFSET)

    # Create coordinator
    coordinator = HomeworksCoordinator(
        hass=hass,
        config=client_config,
        controller_id=controller_id,
        kls_poll_interval=timedelta(seconds=kls_poll_interval),
        kls_window_offset=kls_window_offset,
    )

    # Register CCO devices from options
    _register_cco_devices_from_options(coordinator, options)

    # Register dimmers
    for dimmer in options.get(CONF_DIMMERS, []):
        coordinator.register_dimmer(dimmer[CONF_ADDR])

    # Connect and start coordinator
    try:
        if not await coordinator.async_setup():
            raise ConfigEntryNotReady("Failed to connect to Homeworks controller")
    except Exception as err:
        _LOGGER.error("Failed to setup Homeworks: %s", err)
        # Check if this is an auth failure
        if "auth" in str(err).lower() or "credential" in str(err).lower():
            raise ConfigEntryAuthFailed("Authentication failed") from err
        raise ConfigEntryNotReady(f"Connection failed: {err}") from err

    # Store data
    hass.data[DOMAIN][entry.entry_id] = HomeworksData(
        coordinator=coordinator,
        controller_id=controller_id,
    )

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Handle HA stop
    async def cleanup(event: Event) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, cleanup))
    entry.async_on_unload(entry.add_update_listener(update_listener))

    # Start the coordinator's regular updates
    await coordinator.async_config_entry_first_refresh()

    return True


def _register_cco_devices_from_options(coordinator: HomeworksCoordinator, options: dict[str, Any]) -> None:
    """Register CCO devices from the config entry options.

    Handles both new-style CCO_DEVICES and legacy CCOS/COVERS/LOCKS format.
    """
    # New-style unified CCO devices
    for device_config in options.get(CONF_CCO_DEVICES, []):
        try:
            entity_type_str = device_config.get(CONF_ENTITY_TYPE, CCO_TYPE_SWITCH)
            entity_type = _parse_entity_type(entity_type_str)

            addr_str = device_config[CONF_ADDR]
            button = device_config.get(CONF_BUTTON_NUMBER, device_config.get(CONF_RELAY_NUMBER, 1))

            if "," not in addr_str:
                full_addr = f"{addr_str},{button}"
            else:
                full_addr = addr_str

            address = CCOAddress.from_string(full_addr)

            device = CCODevice(
                address=address,
                name=device_config.get(CONF_NAME, ""),
                entity_type=entity_type,
                inverted=device_config.get(CONF_INVERTED, False),
            )
            coordinator.register_cco_device(device)
        except Exception as err:
            _LOGGER.error("Failed to register CCO device: %s - %s", device_config, err)

    # Legacy CCO format (switches)
    for cco_config in options.get(CONF_CCOS, []):
        try:
            addr = normalize_address(cco_config[CONF_ADDR])
            relay = cco_config.get(CONF_RELAY_NUMBER, 1)
            parts = addr.strip("[]").split(":")

            address = CCOAddress(
                processor=int(parts[0]),
                link=int(parts[1]),
                address=int(parts[2]),
                button=relay,
            )

            device = CCODevice(
                address=address,
                name=cco_config.get(CONF_NAME, ""),
                entity_type=CCOEntityType.SWITCH,
                inverted=cco_config.get(CONF_INVERTED, False),
            )
            coordinator.register_cco_device(device)
        except Exception as err:
            _LOGGER.error("Failed to register legacy CCO: %s - %s", cco_config, err)

    # Legacy covers
    for cover_config in options.get(CONF_COVERS, []):
        try:
            addr = normalize_address(cover_config[CONF_ADDR])
            parts = addr.strip("[]").split(":")

            address = CCOAddress(
                processor=int(parts[0]),
                link=int(parts[1]),
                address=int(parts[2]),
                button=1,
            )

            device = CCODevice(
                address=address,
                name=cover_config.get(CONF_NAME, ""),
                entity_type=CCOEntityType.COVER,
                inverted=cover_config.get(CONF_INVERTED, False),
            )
            coordinator.register_cco_device(device)
        except Exception as err:
            _LOGGER.error("Failed to register legacy cover: %s - %s", cover_config, err)

    # Legacy locks
    for lock_config in options.get(CONF_LOCKS, []):
        try:
            addr = normalize_address(lock_config[CONF_ADDR])
            relay = lock_config.get(CONF_RELAY_NUMBER, 1)
            parts = addr.strip("[]").split(":")

            address = CCOAddress(
                processor=int(parts[0]),
                link=int(parts[1]),
                address=int(parts[2]),
                button=relay,
            )

            device = CCODevice(
                address=address,
                name=lock_config.get(CONF_NAME, ""),
                entity_type=CCOEntityType.LOCK,
                inverted=lock_config.get(CONF_INVERTED, False),
            )
            coordinator.register_cco_device(device)
        except Exception as err:
            _LOGGER.error("Failed to register legacy lock: %s - %s", lock_config, err)


def _parse_entity_type(type_str: str) -> CCOEntityType:
    """Parse entity type string to enum."""
    type_map = {
        CCO_TYPE_SWITCH: CCOEntityType.SWITCH,
        CCO_TYPE_LIGHT: CCOEntityType.LIGHT,
        CCO_TYPE_COVER: CCOEntityType.COVER,
        CCO_TYPE_LOCK: CCOEntityType.LOCK,
    }
    return type_map.get(type_str.lower(), CCOEntityType.SWITCH)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    data: HomeworksData = hass.data[DOMAIN].pop(entry.entry_id)
    await data.coordinator.async_shutdown()

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


def calculate_unique_id(controller_id: str, addr: str, idx: int) -> str:
    """Calculate entity unique id."""
    return f"homeworks.{controller_id}.{addr}.{idx}"


class HomeworksEntity(Entity):
    """Base class of a Homeworks device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: HomeworksCoordinator,
        controller_id: str,
        addr: str,
        idx: int,
        name: str | None,
    ) -> None:
        """Initialize Homeworks device."""
        self._addr = addr
        self._idx = idx
        self._controller_id = controller_id
        self._coordinator = coordinator
        self._attr_name = name
        self._attr_unique_id = calculate_unique_id(self._controller_id, self._addr, self._idx)
        self._attr_extra_state_attributes = {"homeworks_address": self._addr}

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._coordinator.connected
