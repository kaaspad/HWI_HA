"""Lutron Homeworks Series 4 and 8 config flow."""

from __future__ import annotations

import asyncio
import csv
from functools import partial
from io import StringIO
import logging
from typing import Any, NamedTuple

import voluptuous as vol

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import async_get_hass, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    selector,
)
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaCommonFlowHandler,
    SchemaFlowError,
    SchemaFlowFormStep,
    SchemaFlowMenuStep,
    SchemaOptionsFlowHandler,
)
from homeassistant.helpers.selector import TextSelector
from homeassistant.helpers.typing import VolDictType
from homeassistant.util import slugify

from .client import HomeworksClient, HomeworksClientConfig
from .const import (
    CONF_ADDR,
    CONF_BUTTON_NUMBER,
    CONF_BUTTONS,
    CONF_CCO_DEVICES,
    CONF_CCOS,
    CONF_CONTROLLER_ID,
    CONF_COVERS,
    CONF_DIMMERS,
    CONF_ENTITY_TYPE,
    CONF_INDEX,
    CONF_INVERTED,
    CONF_KEYPADS,
    CONF_LED,
    CONF_LOCKS,
    CONF_NUMBER,
    CONF_RATE,
    CONF_RELAY_NUMBER,
    CONF_RELEASE_DELAY,
    CCO_TYPE_COVER,
    CCO_TYPE_LIGHT,
    CCO_TYPE_LOCK,
    CCO_TYPE_SWITCH,
    DEFAULT_BUTTON_NAME,
    DEFAULT_CCO_NAME,
    DEFAULT_COVER_NAME,
    DEFAULT_FADE_RATE,
    DEFAULT_KEYPAD_NAME,
    DEFAULT_KLS_POLL_INTERVAL,
    DEFAULT_LIGHT_NAME,
    DOMAIN,
)
from .models import CCOAddress, CCO_BUTTON_WINDOW_OFFSET, normalize_address

_LOGGER = logging.getLogger(__name__)

# Configuration keys for controller settings
CONF_KLS_POLL_INTERVAL = "kls_poll_interval"
CONF_KLS_WINDOW_OFFSET = "kls_window_offset"

# CCO device types for selector
CCO_ENTITY_TYPES = [
    selector.SelectOptionDict(value=CCO_TYPE_SWITCH, label="switch"),
    selector.SelectOptionDict(value=CCO_TYPE_LIGHT, label="light"),
    selector.SelectOptionDict(value=CCO_TYPE_COVER, label="cover"),
    selector.SelectOptionDict(value=CCO_TYPE_LOCK, label="lock"),
]


# === Connection Testing ===

async def _try_connection(user_input: dict[str, Any]) -> None:
    """Try connecting to the controller."""
    hass = async_get_hass()

    config = HomeworksClientConfig(
        host=user_input[CONF_HOST],
        port=user_input[CONF_PORT],
        username=user_input.get(CONF_USERNAME),
        password=user_input.get(CONF_PASSWORD),
    )

    client = HomeworksClient(config)

    try:
        connected = await client.connect()
        if not connected:
            raise SchemaFlowError("connection_error")
        await client.stop()
    except Exception as err:
        _LOGGER.debug("Connection failed: %s", err)
        raise SchemaFlowError("connection_error") from err


# === Validation Functions ===

async def validate_add_controller(
    handler: ConfigFlow | SchemaOptionsFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate controller setup."""
    user_input[CONF_CONTROLLER_ID] = slugify(user_input[CONF_NAME])
    user_input[CONF_PORT] = int(user_input[CONF_PORT])

    try:
        handler._async_abort_entries_match(
            {CONF_HOST: user_input[CONF_HOST], CONF_PORT: user_input[CONF_PORT]}
        )
    except AbortFlow as err:
        raise SchemaFlowError("duplicated_host_port") from err

    try:
        handler._async_abort_entries_match(
            {CONF_CONTROLLER_ID: user_input[CONF_CONTROLLER_ID]}
        )
    except AbortFlow as err:
        raise SchemaFlowError("duplicated_controller_id") from err

    await _try_connection(user_input)

    return user_input


def _validate_address(
    handler: SchemaCommonFlowHandler,
    addr: str,
    button: int | None = None,
    exclude_index: int | None = None,
) -> str:
    """Validate and normalize address format."""
    try:
        normalized = normalize_address(addr)
        parts = normalized.strip("[]").split(":")
        if len(parts) not in (3, 4, 5) or not all(len(p) == 2 for p in parts):
            raise SchemaFlowError("invalid_addr")
        return normalized
    except ValueError:
        raise SchemaFlowError("invalid_addr")


def _validate_cco_address(addr_str: str, button: int) -> CCOAddress:
    """Validate and parse a CCO address."""
    try:
        # Handle address with or without button
        if "," not in addr_str:
            full_addr = f"{addr_str},{button}"
        else:
            full_addr = addr_str

        return CCOAddress.from_string(full_addr)
    except ValueError as err:
        raise SchemaFlowError("invalid_addr") from err


# === CCO Device CRUD ===

async def validate_add_cco_device(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate CCO device input."""
    addr = user_input[CONF_ADDR]
    button = int(user_input.get(CONF_BUTTON_NUMBER, user_input.get(CONF_RELAY_NUMBER, 1)))

    # Validate and parse address
    cco_addr = _validate_cco_address(addr, button)

    # Check for duplicates
    for idx, device in enumerate(handler.options.get(CONF_CCO_DEVICES, [])):
        existing_addr = _validate_cco_address(
            device[CONF_ADDR],
            device.get(CONF_BUTTON_NUMBER, device.get(CONF_RELAY_NUMBER, 1))
        )
        if existing_addr.unique_key == cco_addr.unique_key:
            raise SchemaFlowError("duplicate_cco")

    # Normalize address
    user_input[CONF_ADDR] = cco_addr.to_kls_address()
    user_input[CONF_BUTTON_NUMBER] = button

    # Ensure list exists and add
    items = handler.options.setdefault(CONF_CCO_DEVICES, [])
    items.append(user_input)
    return {}


async def get_select_cco_device_schema(
    handler: SchemaCommonFlowHandler,
) -> vol.Schema:
    """Return schema for selecting a CCO device."""
    devices = handler.options.get(CONF_CCO_DEVICES, [])
    if not devices:
        raise SchemaFlowError("no_devices")

    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config.get(CONF_NAME, 'CCO')} ({config[CONF_ADDR]}:{config.get(CONF_BUTTON_NUMBER, config.get(CONF_RELAY_NUMBER, 1))}) [{config.get(CONF_ENTITY_TYPE, 'switch')}]"
                    for index, config in enumerate(devices)
                },
            )
        }
    )


async def validate_select_cco_device(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store CCO device index in flow state."""
    handler.flow_state["_cco_idx"] = int(user_input[CONF_INDEX])
    return {}


async def get_edit_cco_device_suggested_values(
    handler: SchemaCommonFlowHandler,
) -> dict[str, Any]:
    """Return suggested values for CCO device editing."""
    idx = handler.flow_state["_cco_idx"]
    device = handler.options[CONF_CCO_DEVICES][idx]
    return {
        CONF_NAME: device.get(CONF_NAME, ""),
        CONF_ADDR: device.get(CONF_ADDR, ""),
        CONF_BUTTON_NUMBER: device.get(CONF_BUTTON_NUMBER, device.get(CONF_RELAY_NUMBER, 1)),
        CONF_ENTITY_TYPE: device.get(CONF_ENTITY_TYPE, CCO_TYPE_SWITCH),
        CONF_INVERTED: device.get(CONF_INVERTED, False),
    }


async def validate_cco_device_edit(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Update edited CCO device."""
    idx = handler.flow_state["_cco_idx"]

    # If address/button changed, validate the new address
    if CONF_ADDR in user_input:
        addr = user_input[CONF_ADDR]
        button = int(user_input.get(CONF_BUTTON_NUMBER, 1))
        cco_addr = _validate_cco_address(addr, button)

        # Check for duplicates (excluding current device)
        for i, device in enumerate(handler.options.get(CONF_CCO_DEVICES, [])):
            if i == idx:
                continue
            existing_addr = _validate_cco_address(
                device[CONF_ADDR],
                device.get(CONF_BUTTON_NUMBER, device.get(CONF_RELAY_NUMBER, 1))
            )
            if existing_addr.unique_key == cco_addr.unique_key:
                raise SchemaFlowError("duplicate_cco")

        user_input[CONF_ADDR] = cco_addr.to_kls_address()
        user_input[CONF_BUTTON_NUMBER] = button

    handler.options[CONF_CCO_DEVICES][idx].update(user_input)
    return {}


async def get_remove_cco_device_schema(
    handler: SchemaCommonFlowHandler,
) -> vol.Schema:
    """Return schema for CCO device removal."""
    devices = handler.options.get(CONF_CCO_DEVICES, [])
    if not devices:
        raise SchemaFlowError("no_devices")

    return vol.Schema(
        {
            vol.Required(CONF_INDEX): cv.multi_select(
                {
                    str(index): f"{config.get(CONF_NAME, 'CCO')} ({config[CONF_ADDR]}:{config.get(CONF_BUTTON_NUMBER, config.get(CONF_RELAY_NUMBER, 1))})"
                    for index, config in enumerate(devices)
                },
            )
        }
    )


async def validate_remove_cco_device(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Remove selected CCO devices."""
    removed_indexes = set(user_input[CONF_INDEX])
    entity_registry = er.async_get(handler.parent_handler.hass)

    new_devices = []
    for index, device in enumerate(handler.options.get(CONF_CCO_DEVICES, [])):
        if str(index) not in removed_indexes:
            new_devices.append(device)
            continue

        # Remove entities from registry
        entity_type = device.get(CONF_ENTITY_TYPE, CCO_TYPE_SWITCH)
        controller_id = handler.options.get(CONF_CONTROLLER_ID, "")
        addr = device[CONF_ADDR]
        button = device.get(CONF_BUTTON_NUMBER, device.get(CONF_RELAY_NUMBER, 1))

        # Try to remove from entity registry
        for entity_id in list(entity_registry.entities):
            entity = entity_registry.entities[entity_id]
            if entity.platform == DOMAIN and addr in (entity.unique_id or ""):
                entity_registry.async_remove(entity_id)

    handler.options[CONF_CCO_DEVICES] = new_devices
    return {}


# === Dimmable Light CRUD ===

async def validate_add_light(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate light input."""
    user_input[CONF_ADDR] = _validate_address(handler, user_input[CONF_ADDR])

    # Check for duplicates
    for item in handler.options.get(CONF_DIMMERS, []):
        if normalize_address(item[CONF_ADDR]) == user_input[CONF_ADDR]:
            raise SchemaFlowError("duplicated_addr")

    items = handler.options.setdefault(CONF_DIMMERS, [])
    items.append(user_input)
    return {}


async def get_select_light_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a light."""
    lights = handler.options.get(CONF_DIMMERS, [])
    if not lights:
        raise SchemaFlowError("no_devices")

    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config.get(CONF_NAME, 'Light')} ({config[CONF_ADDR]})"
                    for index, config in enumerate(lights)
                },
            )
        }
    )


async def validate_select_light(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store light index in flow state."""
    handler.flow_state["_idx"] = int(user_input[CONF_INDEX])
    return {}


async def get_edit_light_suggested_values(
    handler: SchemaCommonFlowHandler,
) -> dict[str, Any]:
    """Return suggested values for light editing."""
    idx = handler.flow_state["_idx"]
    return dict(handler.options[CONF_DIMMERS][idx])


async def validate_light_edit(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Update edited light."""
    idx = handler.flow_state["_idx"]
    handler.options[CONF_DIMMERS][idx].update(user_input)
    return {}


async def get_remove_light_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for light removal."""
    lights = handler.options.get(CONF_DIMMERS, [])
    if not lights:
        raise SchemaFlowError("no_devices")

    return vol.Schema(
        {
            vol.Required(CONF_INDEX): cv.multi_select(
                {
                    str(index): f"{config.get(CONF_NAME, 'Light')} ({config[CONF_ADDR]})"
                    for index, config in enumerate(lights)
                },
            )
        }
    )


async def validate_remove_light(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Remove selected lights."""
    removed_indexes = set(user_input[CONF_INDEX])
    entity_registry = er.async_get(handler.parent_handler.hass)

    new_items = []
    for index, item in enumerate(handler.options.get(CONF_DIMMERS, [])):
        if str(index) not in removed_indexes:
            new_items.append(item)
        else:
            # Remove from entity registry
            for entity_id in list(entity_registry.entities):
                entity = entity_registry.entities[entity_id]
                if entity.platform == DOMAIN and item[CONF_ADDR] in (entity.unique_id or ""):
                    entity_registry.async_remove(entity_id)

    handler.options[CONF_DIMMERS] = new_items
    return {}


# === Keypad CRUD ===

async def validate_add_keypad(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate keypad input."""
    user_input[CONF_ADDR] = _validate_address(handler, user_input[CONF_ADDR])

    # Check for duplicates
    for item in handler.options.get(CONF_KEYPADS, []):
        if normalize_address(item[CONF_ADDR]) == user_input[CONF_ADDR]:
            raise SchemaFlowError("duplicated_addr")

    items = handler.options.setdefault(CONF_KEYPADS, [])
    items.append(user_input | {CONF_BUTTONS: []})
    return {}


async def get_select_keypad_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a keypad."""
    keypads = handler.options.get(CONF_KEYPADS, [])
    if not keypads:
        raise SchemaFlowError("no_devices")

    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config.get(CONF_NAME, 'Keypad')} ({config[CONF_ADDR]})"
                    for index, config in enumerate(keypads)
                },
            )
        }
    )


async def validate_select_keypad(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store keypad index in flow state."""
    handler.flow_state["_idx"] = int(user_input[CONF_INDEX])
    return {}


async def get_remove_keypad_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for keypad removal."""
    keypads = handler.options.get(CONF_KEYPADS, [])
    if not keypads:
        raise SchemaFlowError("no_devices")

    return vol.Schema(
        {
            vol.Required(CONF_INDEX): cv.multi_select(
                {
                    str(index): f"{config.get(CONF_NAME, 'Keypad')} ({config[CONF_ADDR]})"
                    for index, config in enumerate(keypads)
                },
            )
        }
    )


async def validate_remove_keypad(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Remove selected keypads."""
    removed_indexes = set(user_input[CONF_INDEX])
    entity_registry = er.async_get(handler.parent_handler.hass)

    new_items = []
    for index, item in enumerate(handler.options.get(CONF_KEYPADS, [])):
        if str(index) not in removed_indexes:
            new_items.append(item)
        else:
            # Remove from entity registry
            for entity_id in list(entity_registry.entities):
                entity = entity_registry.entities[entity_id]
                if entity.platform == DOMAIN and item[CONF_ADDR] in (entity.unique_id or ""):
                    entity_registry.async_remove(entity_id)

    handler.options[CONF_KEYPADS] = new_items
    return {}


# === Keypad Button CRUD ===

async def validate_add_button(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate button input."""
    user_input[CONF_NUMBER] = int(user_input[CONF_NUMBER])
    keypad_idx = handler.flow_state["_idx"]
    buttons = handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS]

    # Check for duplicate button number
    for button in buttons:
        if button[CONF_NUMBER] == user_input[CONF_NUMBER]:
            raise SchemaFlowError("duplicated_number")

    buttons.append(user_input)
    return {}


async def get_select_button_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a button."""
    keypad_idx = handler.flow_state["_idx"]
    buttons = handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS]

    if not buttons:
        raise SchemaFlowError("no_buttons")

    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config.get(CONF_NAME, 'Button')} (#{config[CONF_NUMBER]})"
                    for index, config in enumerate(buttons)
                },
            )
        }
    )


async def validate_select_button(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store button index in flow state."""
    handler.flow_state["_button_idx"] = int(user_input[CONF_INDEX])
    return {}


async def get_edit_button_suggested_values(
    handler: SchemaCommonFlowHandler,
) -> dict[str, Any]:
    """Return suggested values for button editing."""
    keypad_idx = handler.flow_state["_idx"]
    button_idx = handler.flow_state["_button_idx"]
    return dict(handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS][button_idx])


async def validate_button_edit(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Update edited button."""
    keypad_idx = handler.flow_state["_idx"]
    button_idx = handler.flow_state["_button_idx"]
    handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS][button_idx].update(user_input)
    return {}


async def get_remove_button_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for button removal."""
    keypad_idx = handler.flow_state["_idx"]
    buttons = handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS]

    if not buttons:
        raise SchemaFlowError("no_buttons")

    return vol.Schema(
        {
            vol.Required(CONF_INDEX): cv.multi_select(
                {
                    str(index): f"{config.get(CONF_NAME, 'Button')} (#{config[CONF_NUMBER]})"
                    for index, config in enumerate(buttons)
                },
            )
        }
    )


async def validate_remove_button(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Remove selected buttons."""
    removed_indexes = set(user_input[CONF_INDEX])
    keypad_idx = handler.flow_state["_idx"]

    new_buttons = []
    for index, button in enumerate(handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS]):
        if str(index) not in removed_indexes:
            new_buttons.append(button)

    handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS] = new_buttons
    return {}


# === Controller Settings ===

async def get_controller_settings_suggested_values(
    handler: SchemaCommonFlowHandler,
) -> dict[str, Any]:
    """Return current controller settings."""
    return {
        CONF_KLS_POLL_INTERVAL: handler.options.get(CONF_KLS_POLL_INTERVAL, DEFAULT_KLS_POLL_INTERVAL),
        CONF_KLS_WINDOW_OFFSET: handler.options.get(CONF_KLS_WINDOW_OFFSET, CCO_BUTTON_WINDOW_OFFSET),
    }


async def validate_controller_settings(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Update controller settings."""
    handler.options[CONF_KLS_POLL_INTERVAL] = int(user_input[CONF_KLS_POLL_INTERVAL])
    handler.options[CONF_KLS_WINDOW_OFFSET] = int(user_input[CONF_KLS_WINDOW_OFFSET])
    return {}


# === Review Configuration ===

async def get_review_config_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Generate a read-only configuration summary."""
    # This step just shows data; no actual fields to edit
    return vol.Schema({})


async def validate_review_config(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """No-op validation for review step."""
    return {}


def _build_config_summary(options: dict[str, Any]) -> str:
    """Build a text summary of the configuration."""
    lines = []

    # CCO Devices
    cco_devices = options.get(CONF_CCO_DEVICES, [])
    lines.append(f"**CCO Devices ({len(cco_devices)})**")
    for dev in cco_devices:
        name = dev.get(CONF_NAME, "Unnamed")
        addr = dev.get(CONF_ADDR, "?")
        btn = dev.get(CONF_BUTTON_NUMBER, dev.get(CONF_RELAY_NUMBER, 1))
        etype = dev.get(CONF_ENTITY_TYPE, "switch")
        inv = "inverted" if dev.get(CONF_INVERTED) else ""
        lines.append(f"  - {name}: {addr}:{btn} [{etype}] {inv}")

    # Dimmable Lights
    dimmers = options.get(CONF_DIMMERS, [])
    lines.append(f"\n**Dimmable Lights ({len(dimmers)})**")
    for dim in dimmers:
        name = dim.get(CONF_NAME, "Unnamed")
        addr = dim.get(CONF_ADDR, "?")
        rate = dim.get(CONF_RATE, DEFAULT_FADE_RATE)
        lines.append(f"  - {name}: {addr} (fade: {rate}s)")

    # Keypads
    keypads = options.get(CONF_KEYPADS, [])
    lines.append(f"\n**Keypads ({len(keypads)})**")
    for kp in keypads:
        name = kp.get(CONF_NAME, "Unnamed")
        addr = kp.get(CONF_ADDR, "?")
        btns = kp.get(CONF_BUTTONS, [])
        lines.append(f"  - {name}: {addr} ({len(btns)} buttons)")

    # Settings
    lines.append(f"\n**Settings**")
    lines.append(f"  - KLS Poll Interval: {options.get(CONF_KLS_POLL_INTERVAL, DEFAULT_KLS_POLL_INTERVAL)}s")
    lines.append(f"  - KLS Window Offset: {options.get(CONF_KLS_WINDOW_OFFSET, CCO_BUTTON_WINDOW_OFFSET)}")

    return "\n".join(lines)


# === CSV Import ===

class DeviceImport(NamedTuple):
    """Represents a device import from CSV."""

    device_type: str
    address: str
    button: int | None
    name: str


async def async_parse_csv(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Parse CSV content and store in flow state."""
    content = user_input["csv_file"]
    _LOGGER.debug("Parsing CSV content")

    f = StringIO(content)
    reader = csv.DictReader(f)

    devices = []
    try:
        for row in reader:
            device_type = row.get("device_type", "").strip().upper()

            if device_type in ("CCO", "SWITCH"):
                button = int(row.get("relay", row.get("button", 1)))
                device = DeviceImport(
                    device_type="CCO",
                    address=normalize_address(row["address"].strip()),
                    button=button,
                    name=row.get("name", "").strip(),
                )
            elif device_type == "LIGHT":
                device = DeviceImport(
                    device_type="LIGHT",
                    address=normalize_address(row["address"].strip()),
                    button=None,
                    name=row.get("name", "").strip(),
                )
            elif device_type == "COVER":
                button = int(row.get("relay", row.get("button", 1)))
                device = DeviceImport(
                    device_type="COVER",
                    address=normalize_address(row["address"].strip()),
                    button=button,
                    name=row.get("name", "").strip(),
                )
            elif device_type == "LOCK":
                button = int(row.get("relay", row.get("button", 1)))
                device = DeviceImport(
                    device_type="LOCK",
                    address=normalize_address(row["address"].strip()),
                    button=button,
                    name=row.get("name", "").strip(),
                )
            elif device_type == "DIMMER":
                device = DeviceImport(
                    device_type="DIMMER",
                    address=normalize_address(row["address"].strip()),
                    button=None,
                    name=row.get("name", "").strip(),
                )
            else:
                _LOGGER.warning("Unknown device type: %s", device_type)
                continue

            devices.append(device)
    except Exception as err:
        _LOGGER.exception("Error processing CSV row")
        raise SchemaFlowError("invalid_csv") from err

    if not devices:
        raise SchemaFlowError("no_devices_in_csv")

    _LOGGER.debug("Found %d devices to import", len(devices))
    handler.flow_state["import_devices"] = devices
    return {}


async def get_confirm_import_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for confirming device imports."""
    devices = handler.flow_state.get("import_devices", [])

    # Create selection dictionary
    selections = {}
    for idx, dev in enumerate(devices):
        if dev.device_type in ("LIGHT", "DIMMER"):
            desc = f"Dimmer: {dev.name} ({dev.address})"
        else:
            desc = f"{dev.device_type}: {dev.name} ({dev.address}:{dev.button})"
        selections[str(idx)] = desc

    return vol.Schema(
        {vol.Optional("devices", default=list(selections.keys())): cv.multi_select(selections)}
    )


async def validate_confirm_import(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Process the selected devices for import."""
    devices = handler.flow_state.get("import_devices", [])
    selected_indices = user_input.get("devices", [])

    for idx in selected_indices:
        device = devices[int(idx)]

        if device.device_type in ("LIGHT", "DIMMER"):
            items = handler.options.setdefault(CONF_DIMMERS, [])
            items.append(
                {
                    CONF_ADDR: device.address,
                    CONF_NAME: device.name or DEFAULT_LIGHT_NAME,
                    CONF_RATE: DEFAULT_FADE_RATE,
                }
            )
        else:
            # CCO-based device
            entity_type_map = {
                "CCO": CCO_TYPE_SWITCH,
                "SWITCH": CCO_TYPE_SWITCH,
                "COVER": CCO_TYPE_COVER,
                "LOCK": CCO_TYPE_LOCK,
            }
            entity_type = entity_type_map.get(device.device_type, CCO_TYPE_SWITCH)

            items = handler.options.setdefault(CONF_CCO_DEVICES, [])
            items.append(
                {
                    CONF_ADDR: device.address,
                    CONF_BUTTON_NUMBER: device.button or 1,
                    CONF_NAME: device.name or DEFAULT_CCO_NAME,
                    CONF_ENTITY_TYPE: entity_type,
                    CONF_INVERTED: False,
                }
            )

    return {}


# === Schema Definitions ===

# Controller configuration schema
CONTROLLER_EDIT = {
    vol.Required(CONF_HOST): selector.TextSelector(),
    vol.Required(CONF_PORT): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=1,
            max=65535,
            mode=selector.NumberSelectorMode.BOX,
        )
    ),
    vol.Optional(CONF_USERNAME): selector.TextSelector(),
    vol.Optional(CONF_PASSWORD): selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    ),
}

DATA_SCHEMA_ADD_CONTROLLER = vol.Schema(
    {
        vol.Required(
            CONF_NAME, description={"suggested_value": "Lutron Homeworks"}
        ): selector.TextSelector(),
        **CONTROLLER_EDIT,
    }
)

DATA_SCHEMA_EDIT_CONTROLLER = vol.Schema(CONTROLLER_EDIT)

DATA_SCHEMA_REAUTH = vol.Schema(
    {
        vol.Optional(CONF_USERNAME): selector.TextSelector(),
        vol.Optional(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)

# Light (dimmer) schemas
LIGHT_EDIT: VolDictType = {
    vol.Optional(CONF_RATE, default=DEFAULT_FADE_RATE): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=20,
            mode=selector.NumberSelectorMode.SLIDER,
            step=0.1,
            unit_of_measurement="s",
        )
    ),
}

DATA_SCHEMA_ADD_LIGHT = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_LIGHT_NAME): selector.TextSelector(),
        vol.Required(CONF_ADDR): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.TEXT,
            )
        ),
        **LIGHT_EDIT,
    }
)

DATA_SCHEMA_EDIT_LIGHT = vol.Schema(
    {
        vol.Optional(CONF_NAME): selector.TextSelector(),
        **LIGHT_EDIT,
    }
)

# Keypad button schemas
BUTTON_EDIT: VolDictType = {
    vol.Optional(CONF_LED, default=False): selector.BooleanSelector(),
    vol.Optional(CONF_RELEASE_DELAY, default=0): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=5,
            step=0.01,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        ),
    ),
}

DATA_SCHEMA_ADD_BUTTON = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_BUTTON_NAME): selector.TextSelector(),
        vol.Required(CONF_NUMBER): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=24,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
            ),
        ),
        **BUTTON_EDIT,
    }
)

DATA_SCHEMA_EDIT_BUTTON = vol.Schema(
    {
        vol.Optional(CONF_NAME): selector.TextSelector(),
        **BUTTON_EDIT,
    }
)

# Keypad schemas
DATA_SCHEMA_ADD_KEYPAD = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_KEYPAD_NAME): selector.TextSelector(),
        vol.Required(CONF_ADDR): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.TEXT,
            )
        ),
    }
)

# CCO device schemas
DATA_SCHEMA_ADD_CCO_DEVICE = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_CCO_NAME): selector.TextSelector(),
        vol.Required(CONF_ADDR): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.TEXT,
            )
        ),
        vol.Required(CONF_BUTTON_NUMBER, default=1): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=24,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
            ),
        ),
        vol.Required(CONF_ENTITY_TYPE, default=CCO_TYPE_SWITCH): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CCO_ENTITY_TYPES,
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key="cco_entity_type",
            ),
        ),
        vol.Optional(CONF_INVERTED, default=False): selector.BooleanSelector(),
    }
)

DATA_SCHEMA_EDIT_CCO_DEVICE = vol.Schema(
    {
        vol.Optional(CONF_NAME): selector.TextSelector(),
        vol.Optional(CONF_ADDR): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.TEXT,
            )
        ),
        vol.Optional(CONF_BUTTON_NUMBER): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=24,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
            ),
        ),
        vol.Optional(CONF_ENTITY_TYPE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CCO_ENTITY_TYPES,
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key="cco_entity_type",
            ),
        ),
        vol.Optional(CONF_INVERTED): selector.BooleanSelector(),
    }
)

# Controller settings schema
DATA_SCHEMA_CONTROLLER_SETTINGS = vol.Schema(
    {
        vol.Required(CONF_KLS_POLL_INTERVAL, default=DEFAULT_KLS_POLL_INTERVAL): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=5,
                max=300,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="s",
            ),
        ),
        vol.Required(CONF_KLS_WINDOW_OFFSET, default=CCO_BUTTON_WINDOW_OFFSET): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=16,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
            ),
        ),
    }
)


# === Options Flow Definition ===

OPTIONS_FLOW = {
    "init": SchemaFlowMenuStep(
        [
            "manage_cco_devices",
            "manage_dimmers",
            "manage_keypads",
            "controller_settings",
            "import_csv",
            "review_config",
        ]
    ),
    # CCO Device Management Sub-menu
    "manage_cco_devices": SchemaFlowMenuStep(
        [
            "add_cco_device",
            "select_edit_cco_device",
            "remove_cco_device",
        ]
    ),
    "add_cco_device": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_CCO_DEVICE,
        validate_user_input=validate_add_cco_device,
    ),
    "select_edit_cco_device": SchemaFlowFormStep(
        get_select_cco_device_schema,
        validate_user_input=validate_select_cco_device,
        next_step="edit_cco_device",
    ),
    "edit_cco_device": SchemaFlowFormStep(
        DATA_SCHEMA_EDIT_CCO_DEVICE,
        suggested_values=get_edit_cco_device_suggested_values,
        validate_user_input=validate_cco_device_edit,
    ),
    "remove_cco_device": SchemaFlowFormStep(
        get_remove_cco_device_schema,
        validate_user_input=validate_remove_cco_device,
    ),
    # Dimmer Management Sub-menu
    "manage_dimmers": SchemaFlowMenuStep(
        [
            "add_light",
            "select_edit_light",
            "remove_light",
        ]
    ),
    "add_light": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_LIGHT,
        validate_user_input=validate_add_light,
    ),
    "select_edit_light": SchemaFlowFormStep(
        get_select_light_schema,
        validate_user_input=validate_select_light,
        next_step="edit_light",
    ),
    "edit_light": SchemaFlowFormStep(
        DATA_SCHEMA_EDIT_LIGHT,
        suggested_values=get_edit_light_suggested_values,
        validate_user_input=validate_light_edit,
    ),
    "remove_light": SchemaFlowFormStep(
        get_remove_light_schema,
        validate_user_input=validate_remove_light,
    ),
    # Keypad Management Sub-menu
    "manage_keypads": SchemaFlowMenuStep(
        [
            "add_keypad",
            "select_edit_keypad",
            "remove_keypad",
        ]
    ),
    "add_keypad": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_KEYPAD,
        validate_user_input=validate_add_keypad,
    ),
    "select_edit_keypad": SchemaFlowFormStep(
        get_select_keypad_schema,
        validate_user_input=validate_select_keypad,
        next_step="edit_keypad",
    ),
    "edit_keypad": SchemaFlowMenuStep(
        [
            "add_button",
            "select_edit_button",
            "remove_button",
        ]
    ),
    "remove_keypad": SchemaFlowFormStep(
        get_remove_keypad_schema,
        validate_user_input=validate_remove_keypad,
    ),
    # Keypad Buttons
    "add_button": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_BUTTON,
        validate_user_input=validate_add_button,
    ),
    "select_edit_button": SchemaFlowFormStep(
        get_select_button_schema,
        validate_user_input=validate_select_button,
        next_step="edit_button",
    ),
    "edit_button": SchemaFlowFormStep(
        DATA_SCHEMA_EDIT_BUTTON,
        suggested_values=get_edit_button_suggested_values,
        validate_user_input=validate_button_edit,
    ),
    "remove_button": SchemaFlowFormStep(
        get_remove_button_schema,
        validate_user_input=validate_remove_button,
    ),
    # Controller Settings
    "controller_settings": SchemaFlowFormStep(
        DATA_SCHEMA_CONTROLLER_SETTINGS,
        suggested_values=get_controller_settings_suggested_values,
        validate_user_input=validate_controller_settings,
    ),
    # CSV Import
    "import_csv": SchemaFlowFormStep(
        vol.Schema(
            {
                vol.Required("csv_file"): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True, type=selector.TextSelectorType.TEXT)
                )
            }
        ),
        validate_user_input=async_parse_csv,
        next_step="confirm_import",
    ),
    "confirm_import": SchemaFlowFormStep(
        get_confirm_import_schema,
        validate_user_input=validate_confirm_import,
    ),
    # Review Configuration (read-only)
    "review_config": SchemaFlowFormStep(
        get_review_config_schema,
        validate_user_input=validate_review_config,
    ),
}


class HomeworksConfigFlowHandler(ConfigFlow, domain=DOMAIN):
    """Config flow for Lutron Homeworks."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._reauth_entry: ConfigEntry | None = None

    async def _validate_edit_controller(
        self, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate controller setup."""
        user_input[CONF_PORT] = int(user_input[CONF_PORT])

        our_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert our_entry
        other_entries = self._async_current_entries()
        for entry in other_entries:
            if entry.entry_id == our_entry.entry_id:
                continue
            if (
                user_input[CONF_HOST] == entry.options[CONF_HOST]
                and user_input[CONF_PORT] == entry.options[CONF_PORT]
            ):
                raise SchemaFlowError("duplicated_host_port")

        await _try_connection(user_input)
        return user_input

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        errors = {}
        if user_input:
            try:
                await validate_add_controller(self, user_input)
            except SchemaFlowError as err:
                errors["base"] = str(err)
            else:
                self._async_abort_entries_match(
                    {CONF_HOST: user_input[CONF_HOST], CONF_PORT: user_input[CONF_PORT]}
                )
                name = user_input.pop(CONF_NAME)
                user_input |= {
                    CONF_DIMMERS: [],
                    CONF_KEYPADS: [],
                    CONF_CCO_DEVICES: [],
                    CONF_KLS_POLL_INTERVAL: DEFAULT_KLS_POLL_INTERVAL,
                    CONF_KLS_WINDOW_OFFSET: CCO_BUTTON_WINDOW_OFFSET,
                    # Keep legacy keys for migration
                    CONF_CCOS: [],
                    CONF_COVERS: [],
                    CONF_LOCKS: [],
                }
                return self.async_create_entry(title=name, data={}, options=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA_ADD_CONTROLLER,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthorization request."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauthorization confirmation."""
        errors = {}

        if user_input is not None:
            assert self._reauth_entry is not None

            # Build new options with updated credentials
            new_options = dict(self._reauth_entry.options)
            new_options[CONF_USERNAME] = user_input.get(CONF_USERNAME)
            new_options[CONF_PASSWORD] = user_input.get(CONF_PASSWORD)

            try:
                await _try_connection(new_options)
            except SchemaFlowError as err:
                errors["base"] = str(err)
            else:
                return self.async_update_reload_and_abort(
                    self._reauth_entry,
                    options=new_options,
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=DATA_SCHEMA_REAUTH,
            errors=errors,
            description_placeholders={
                "host": self._reauth_entry.options.get(CONF_HOST, "") if self._reauth_entry else "",
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a reconfigure flow."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry

        errors = {}
        suggested_values = {
            CONF_HOST: entry.options[CONF_HOST],
            CONF_PORT: entry.options[CONF_PORT],
            CONF_USERNAME: entry.options.get(CONF_USERNAME),
            CONF_PASSWORD: entry.options.get(CONF_PASSWORD),
        }

        if user_input:
            suggested_values = {
                CONF_HOST: user_input[CONF_HOST],
                CONF_PORT: user_input[CONF_PORT],
                CONF_USERNAME: user_input.get(CONF_USERNAME),
                CONF_PASSWORD: user_input.get(CONF_PASSWORD),
            }
            try:
                await self._validate_edit_controller(user_input)
            except SchemaFlowError as err:
                errors["base"] = str(err)
            else:
                new_options = entry.options | {
                    CONF_HOST: user_input[CONF_HOST],
                    CONF_PORT: user_input[CONF_PORT],
                    CONF_USERNAME: user_input.get(CONF_USERNAME),
                    CONF_PASSWORD: user_input.get(CONF_PASSWORD),
                }
                return self.async_update_reload_and_abort(
                    entry,
                    options=new_options,
                    reason="reconfigure_successful",
                    reload_even_if_entry_is_unchanged=False,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                DATA_SCHEMA_EDIT_CONTROLLER, suggested_values
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SchemaOptionsFlowHandler:
        """Options flow handler for Lutron Homeworks."""
        return SchemaOptionsFlowHandler(config_entry, OPTIONS_FLOW)
