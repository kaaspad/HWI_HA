"""Lutron Homeworks Series 4 and 8 config flow.

HA 2026.1 compliant:
- Credentials (host, port, username, password) stored in entry.data
- Non-secrets (devices, settings) stored in entry.options
"""

from __future__ import annotations

import csv
from io import StringIO
import logging
from typing import Any, NamedTuple

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import callback
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
from homeassistant.helpers.typing import VolDictType
from homeassistant.util import slugify

from .const import (
    CONF_ADDR,
    CONF_AREA,
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
    CONF_KLS_POLL_INTERVAL,
    CONF_KLS_WINDOW_OFFSET,
    CONF_LED,
    CONF_LOCKS,
    CONF_NUMBER,
    CONF_RATE,
    CONF_RELAY_NUMBER,
    CONF_RELEASE_DELAY,
    CCO_TYPE_CLIMATE,
    CCO_TYPE_COVER,
    CCO_TYPE_FAN,
    CCO_TYPE_LIGHT,
    CCO_TYPE_LOCK,
    CCO_TYPE_SWITCH,
    DEFAULT_BUTTON_NAME,
    DEFAULT_CCO_NAME,
    DEFAULT_FADE_RATE,
    DEFAULT_KEYPAD_NAME,
    DEFAULT_KLS_POLL_INTERVAL,
    DEFAULT_KLS_WINDOW_OFFSET,
    DEFAULT_LIGHT_NAME,
    DOMAIN,
)
from .models import CCOAddress, normalize_address

_LOGGER = logging.getLogger(__name__)

# CCO device types for selector
CCO_ENTITY_TYPES = [
    selector.SelectOptionDict(value=CCO_TYPE_SWITCH, label="switch"),
    selector.SelectOptionDict(value=CCO_TYPE_LIGHT, label="light"),
    selector.SelectOptionDict(value=CCO_TYPE_COVER, label="cover"),
    selector.SelectOptionDict(value=CCO_TYPE_LOCK, label="lock"),
    selector.SelectOptionDict(value=CCO_TYPE_CLIMATE, label="climate"),
    selector.SelectOptionDict(value=CCO_TYPE_FAN, label="fan"),
]


# === Connection Testing ===


async def _try_connection(
    host: str,
    port: int,
    username: str | None = None,
    password: str | None = None,
) -> None:
    """Try connecting to the controller."""
    from .client import HomeworksClient, HomeworksClientConfig

    config = HomeworksClientConfig(
        host=host,
        port=port,
        username=username,
        password=password,
    )

    client = HomeworksClient(config)

    try:
        connected = await client.connect()
        if not connected:
            raise SchemaFlowError("connection_error")
        await client.stop()
    except SchemaFlowError:
        raise
    except Exception as err:
        _LOGGER.debug("Connection failed: %s", err)
        raise SchemaFlowError("connection_error") from err


# === Validation Functions ===


def _validate_address(addr: str) -> str:
    """Validate and normalize address format."""
    try:
        normalized = normalize_address(addr)
        parts = normalized.strip("[]").split(":")
        if len(parts) not in (3, 4, 5) or not all(len(p) == 2 for p in parts):
            raise SchemaFlowError("invalid_addr")
        return normalized
    except ValueError as err:
        raise SchemaFlowError("invalid_addr") from err


def _validate_cco_address(addr_str: str, button: int) -> CCOAddress:
    """Validate and parse a CCO address."""
    try:
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
    button = int(
        user_input.get(CONF_BUTTON_NUMBER, user_input.get(CONF_RELAY_NUMBER, 1))
    )
    cco_addr = _validate_cco_address(addr, button)

    # Check for duplicates
    for device in handler.options.get(CONF_CCO_DEVICES, []):
        existing_addr = _validate_cco_address(
            device[CONF_ADDR],
            device.get(CONF_BUTTON_NUMBER, device.get(CONF_RELAY_NUMBER, 1)),
        )
        if existing_addr.unique_key == cco_addr.unique_key:
            raise SchemaFlowError("duplicate_cco")

    user_input[CONF_ADDR] = cco_addr.to_kls_address()
    user_input[CONF_BUTTON_NUMBER] = button

    items = handler.options.setdefault(CONF_CCO_DEVICES, [])
    items.append(user_input)
    return {}


async def get_select_cco_device_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a CCO device."""
    devices = handler.options.get(CONF_CCO_DEVICES, [])
    if not devices:
        raise SchemaFlowError("no_devices")

    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(
                        i
                    ): f"{d.get(CONF_NAME, 'CCO')} ({d[CONF_ADDR]}:{d.get(CONF_BUTTON_NUMBER, d.get(CONF_RELAY_NUMBER, 1))}) [{d.get(CONF_ENTITY_TYPE, 'switch')}]"
                    for i, d in enumerate(devices)
                }
            )
        }
    )


async def validate_select_cco_device(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store CCO device index."""
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
        CONF_BUTTON_NUMBER: device.get(
            CONF_BUTTON_NUMBER, device.get(CONF_RELAY_NUMBER, 1)
        ),
        CONF_ENTITY_TYPE: device.get(CONF_ENTITY_TYPE, CCO_TYPE_SWITCH),
        CONF_INVERTED: device.get(CONF_INVERTED, False),
    }


async def validate_cco_device_edit(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Update edited CCO device."""
    idx = handler.flow_state["_cco_idx"]

    if CONF_ADDR in user_input:
        addr = user_input[CONF_ADDR]
        button = int(user_input.get(CONF_BUTTON_NUMBER, 1))
        cco_addr = _validate_cco_address(addr, button)

        # Check for duplicates (excluding current)
        for i, device in enumerate(handler.options.get(CONF_CCO_DEVICES, [])):
            if i == idx:
                continue
            existing_addr = _validate_cco_address(
                device[CONF_ADDR],
                device.get(CONF_BUTTON_NUMBER, device.get(CONF_RELAY_NUMBER, 1)),
            )
            if existing_addr.unique_key == cco_addr.unique_key:
                raise SchemaFlowError("duplicate_cco")

        user_input[CONF_ADDR] = cco_addr.to_kls_address()
        user_input[CONF_BUTTON_NUMBER] = button

    handler.options[CONF_CCO_DEVICES][idx].update(user_input)
    return {}


async def get_remove_cco_device_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for CCO device removal."""
    devices = handler.options.get(CONF_CCO_DEVICES, [])
    if not devices:
        raise SchemaFlowError("no_devices")

    return vol.Schema(
        {
            vol.Required(CONF_INDEX): cv.multi_select(
                {
                    str(
                        i
                    ): f"{d.get(CONF_NAME, 'CCO')} ({d[CONF_ADDR]}:{d.get(CONF_BUTTON_NUMBER, d.get(CONF_RELAY_NUMBER, 1))})"
                    for i, d in enumerate(devices)
                }
            )
        }
    )


async def validate_remove_cco_device(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Remove selected CCO devices."""
    removed = set(user_input[CONF_INDEX])
    registry = er.async_get(handler.parent_handler.hass)

    new_devices = []
    for i, device in enumerate(handler.options.get(CONF_CCO_DEVICES, [])):
        if str(i) not in removed:
            new_devices.append(device)
        else:
            addr = device[CONF_ADDR]
            for entity_id in list(registry.entities):
                entity = registry.entities[entity_id]
                if entity.platform == DOMAIN and addr in (entity.unique_id or ""):
                    registry.async_remove(entity_id)

    handler.options[CONF_CCO_DEVICES] = new_devices
    return {}


# === Dimmable Light CRUD ===


async def validate_add_light(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate light input."""
    user_input[CONF_ADDR] = _validate_address(user_input[CONF_ADDR])

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
                    str(i): f"{d.get(CONF_NAME, 'Light')} ({d[CONF_ADDR]})"
                    for i, d in enumerate(lights)
                }
            )
        }
    )


async def validate_select_light(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store light index."""
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
                    str(i): f"{d.get(CONF_NAME, 'Light')} ({d[CONF_ADDR]})"
                    for i, d in enumerate(lights)
                }
            )
        }
    )


async def validate_remove_light(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Remove selected lights."""
    removed = set(user_input[CONF_INDEX])
    registry = er.async_get(handler.parent_handler.hass)

    new_items = []
    for i, item in enumerate(handler.options.get(CONF_DIMMERS, [])):
        if str(i) not in removed:
            new_items.append(item)
        else:
            for entity_id in list(registry.entities):
                entity = registry.entities[entity_id]
                if entity.platform == DOMAIN and item[CONF_ADDR] in (
                    entity.unique_id or ""
                ):
                    registry.async_remove(entity_id)

    handler.options[CONF_DIMMERS] = new_items
    return {}


# === Keypad CRUD ===


async def validate_add_keypad(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate keypad input."""
    user_input[CONF_ADDR] = _validate_address(user_input[CONF_ADDR])

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
                    str(i): f"{d.get(CONF_NAME, 'Keypad')} ({d[CONF_ADDR]})"
                    for i, d in enumerate(keypads)
                }
            )
        }
    )


async def validate_select_keypad(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store keypad index."""
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
                    str(i): f"{d.get(CONF_NAME, 'Keypad')} ({d[CONF_ADDR]})"
                    for i, d in enumerate(keypads)
                }
            )
        }
    )


async def validate_remove_keypad(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Remove selected keypads."""
    removed = set(user_input[CONF_INDEX])
    registry = er.async_get(handler.parent_handler.hass)

    new_items = []
    for i, item in enumerate(handler.options.get(CONF_KEYPADS, [])):
        if str(i) not in removed:
            new_items.append(item)
        else:
            for entity_id in list(registry.entities):
                entity = registry.entities[entity_id]
                if entity.platform == DOMAIN and item[CONF_ADDR] in (
                    entity.unique_id or ""
                ):
                    registry.async_remove(entity_id)

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
                    str(i): f"{b.get(CONF_NAME, 'Button')} (#{b[CONF_NUMBER]})"
                    for i, b in enumerate(buttons)
                }
            )
        }
    )


async def validate_select_button(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store button index."""
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
    handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS][button_idx].update(
        user_input
    )
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
                    str(i): f"{b.get(CONF_NAME, 'Button')} (#{b[CONF_NUMBER]})"
                    for i, b in enumerate(buttons)
                }
            )
        }
    )


async def validate_remove_button(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Remove selected buttons."""
    removed = set(user_input[CONF_INDEX])
    keypad_idx = handler.flow_state["_idx"]

    new_buttons = []
    for i, button in enumerate(handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS]):
        if str(i) not in removed:
            new_buttons.append(button)

    handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS] = new_buttons
    return {}


# === Controller Settings ===


async def get_controller_settings_suggested_values(
    handler: SchemaCommonFlowHandler,
) -> dict[str, Any]:
    """Return current controller settings."""
    return {
        CONF_KLS_POLL_INTERVAL: handler.options.get(
            CONF_KLS_POLL_INTERVAL, DEFAULT_KLS_POLL_INTERVAL
        ),
        CONF_KLS_WINDOW_OFFSET: handler.options.get(
            CONF_KLS_WINDOW_OFFSET, DEFAULT_KLS_WINDOW_OFFSET
        ),
    }


async def validate_controller_settings(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Update controller settings."""
    handler.options[CONF_KLS_POLL_INTERVAL] = int(user_input[CONF_KLS_POLL_INTERVAL])
    handler.options[CONF_KLS_WINDOW_OFFSET] = int(user_input[CONF_KLS_WINDOW_OFFSET])
    return {}


# === CSV Import ===


class DeviceImport(NamedTuple):
    """Device import from CSV."""

    device_type: str
    address: str
    button: int | None
    name: str
    entity_type: str | None = None  # For CCO: switch/light/cover/lock/climate
    area: str | None = None  # Home Assistant area ID


async def async_parse_csv(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Parse CSV content."""
    content = user_input["csv_file"]
    f = StringIO(content)
    reader = csv.DictReader(f)

    devices = []
    try:
        for row in reader:
            device_type = row.get("device_type", "").strip().upper()
            # Get optional entity type for CCO devices (switch/light/cover/lock/climate)
            cco_type = row.get("type", "").strip().lower() or None
            # Get optional area
            area = row.get("area", "").strip() or None

            if device_type in ("CCO", "SWITCH"):
                button = int(row.get("relay", row.get("button", 1)))
                # Map type column to entity type, default to switch
                entity_type = cco_type if cco_type in (
                    CCO_TYPE_SWITCH, CCO_TYPE_LIGHT, CCO_TYPE_COVER,
                    CCO_TYPE_LOCK, CCO_TYPE_CLIMATE, CCO_TYPE_FAN
                ) else CCO_TYPE_SWITCH
                devices.append(
                    DeviceImport(
                        "CCO",
                        normalize_address(row["address"].strip()),
                        button,
                        row.get("name", "").strip(),
                        entity_type,
                        area,
                    )
                )
            elif device_type in ("LIGHT", "DIMMER"):
                devices.append(
                    DeviceImport(
                        "DIMMER",
                        normalize_address(row["address"].strip()),
                        None,
                        row.get("name", "").strip(),
                        None,
                        area,
                    )
                )
            elif device_type == "COVER":
                button = int(row.get("relay", row.get("button", 1)))
                devices.append(
                    DeviceImport(
                        "CCO",
                        normalize_address(row["address"].strip()),
                        button,
                        row.get("name", "").strip(),
                        CCO_TYPE_COVER,
                        area,
                    )
                )
            elif device_type == "LOCK":
                button = int(row.get("relay", row.get("button", 1)))
                devices.append(
                    DeviceImport(
                        "CCO",
                        normalize_address(row["address"].strip()),
                        button,
                        row.get("name", "").strip(),
                        CCO_TYPE_LOCK,
                        area,
                    )
                )
            elif device_type == "CLIMATE":
                button = int(row.get("relay", row.get("button", 1)))
                devices.append(
                    DeviceImport(
                        "CCO",
                        normalize_address(row["address"].strip()),
                        button,
                        row.get("name", "").strip(),
                        CCO_TYPE_CLIMATE,
                        area,
                    )
                )
            elif device_type == "FAN":
                button = int(row.get("relay", row.get("button", 1)))
                devices.append(
                    DeviceImport(
                        "CCO",
                        normalize_address(row["address"].strip()),
                        button,
                        row.get("name", "").strip(),
                        CCO_TYPE_FAN,
                        area,
                    )
                )
    except Exception as err:
        _LOGGER.exception("Error processing CSV")
        raise SchemaFlowError("invalid_csv") from err

    if not devices:
        raise SchemaFlowError("no_devices_in_csv")

    handler.flow_state["import_devices"] = devices
    return {}


def _is_duplicate_cco(handler: SchemaCommonFlowHandler, address: str, button: int) -> bool:
    """Check if a CCO device already exists."""
    try:
        new_addr = _validate_cco_address(address, button)
        for device in handler.options.get(CONF_CCO_DEVICES, []):
            existing_addr = _validate_cco_address(
                device[CONF_ADDR],
                device.get(CONF_BUTTON_NUMBER, device.get(CONF_RELAY_NUMBER, 1)),
            )
            if existing_addr.unique_key == new_addr.unique_key:
                return True
    except Exception:
        pass
    return False


def _is_duplicate_dimmer(handler: SchemaCommonFlowHandler, address: str) -> bool:
    """Check if a dimmer already exists."""
    normalized = normalize_address(address)
    for dimmer in handler.options.get(CONF_DIMMERS, []):
        if normalize_address(dimmer[CONF_ADDR]) == normalized:
            return True
    return False


async def get_confirm_import_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for confirming imports."""
    devices = handler.flow_state.get("import_devices", [])
    selections = {}
    default_selected = []

    for idx, dev in enumerate(devices):
        if dev.device_type == "DIMMER":
            is_dup = _is_duplicate_dimmer(handler, dev.address)
            label = f"Dimmer: {dev.name} ({dev.address})"
            if is_dup:
                label += " [ALREADY EXISTS]"
            else:
                default_selected.append(str(idx))
            selections[str(idx)] = label
        else:
            entity_type = dev.entity_type or "switch"
            is_dup = _is_duplicate_cco(handler, dev.address, dev.button or 1)
            label = f"CCO ({entity_type}): {dev.name} ({dev.address}:{dev.button})"
            if is_dup:
                label += " [ALREADY EXISTS]"
            else:
                default_selected.append(str(idx))
            selections[str(idx)] = label

    return vol.Schema(
        {
            vol.Optional("devices", default=default_selected): cv.multi_select(
                selections
            )
        }
    )


async def validate_confirm_import(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Process selected devices, skipping duplicates."""
    devices = handler.flow_state.get("import_devices", [])
    selected = user_input.get("devices", [])
    skipped = 0

    for idx in selected:
        device = devices[int(idx)]
        if device.device_type == "DIMMER":
            # Skip if duplicate
            if _is_duplicate_dimmer(handler, device.address):
                skipped += 1
                continue
            items = handler.options.setdefault(CONF_DIMMERS, [])
            dimmer_config = {
                CONF_ADDR: device.address,
                CONF_NAME: device.name or DEFAULT_LIGHT_NAME,
                CONF_RATE: DEFAULT_FADE_RATE,
            }
            if device.area:
                dimmer_config[CONF_AREA] = device.area
            items.append(dimmer_config)
        else:
            # Skip if duplicate
            if _is_duplicate_cco(handler, device.address, device.button or 1):
                skipped += 1
                continue
            # Use entity_type from CSV if provided, otherwise default to switch
            entity_type = device.entity_type or CCO_TYPE_SWITCH
            _LOGGER.debug(
                "Importing CCO device %s with entity_type=%s (from CSV: %s)",
                device.name,
                entity_type,
                device.entity_type,
            )
            items = handler.options.setdefault(CONF_CCO_DEVICES, [])
            cco_config = {
                CONF_ADDR: device.address,
                CONF_BUTTON_NUMBER: device.button or 1,
                CONF_NAME: device.name or DEFAULT_CCO_NAME,
                CONF_ENTITY_TYPE: entity_type,
                CONF_INVERTED: False,
            }
            if device.area:
                cco_config[CONF_AREA] = device.area
            items.append(cco_config)

    return {}


# === Review Configuration ===


async def get_review_config_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return empty schema for review."""
    return vol.Schema({})


async def validate_review_config(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """No-op for review."""
    return {}


# === Schema Definitions ===

DATA_SCHEMA_ADD_CONTROLLER = vol.Schema(
    {
        vol.Required(
            CONF_NAME, description={"suggested_value": "Lutron Homeworks"}
        ): selector.TextSelector(),
        vol.Required(CONF_HOST): selector.TextSelector(),
        vol.Required(CONF_PORT, default=23): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=65535, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Optional(CONF_USERNAME): selector.TextSelector(),
        vol.Optional(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)

DATA_SCHEMA_REAUTH = vol.Schema(
    {
        vol.Optional(CONF_USERNAME): selector.TextSelector(),
        vol.Optional(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)

DATA_SCHEMA_RECONFIGURE = vol.Schema(
    {
        vol.Required(CONF_HOST): selector.TextSelector(),
        vol.Required(CONF_PORT): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=65535, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Optional(CONF_USERNAME): selector.TextSelector(),
        vol.Optional(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)

LIGHT_EDIT: VolDictType = {
    vol.Optional(CONF_RATE, default=DEFAULT_FADE_RATE): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=20,
            mode=selector.NumberSelectorMode.BOX,
            step=0.01,
            unit_of_measurement="s",
        )
    ),
}

DATA_SCHEMA_ADD_LIGHT = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_LIGHT_NAME): selector.TextSelector(),
        vol.Required(CONF_ADDR): selector.TextSelector(),
        vol.Optional(CONF_AREA): selector.AreaSelector(),
        **LIGHT_EDIT,
    }
)

DATA_SCHEMA_EDIT_LIGHT = vol.Schema(
    {vol.Optional(CONF_NAME): selector.TextSelector(), **LIGHT_EDIT}
)

BUTTON_EDIT: VolDictType = {
    vol.Optional(CONF_LED, default=False): selector.BooleanSelector(),
    vol.Optional(CONF_RELEASE_DELAY, default=0): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=5,
            step=0.01,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    ),
}

DATA_SCHEMA_ADD_BUTTON = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_BUTTON_NAME): selector.TextSelector(),
        vol.Required(CONF_NUMBER): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=24, step=1, mode=selector.NumberSelectorMode.BOX
            )
        ),
        **BUTTON_EDIT,
    }
)

DATA_SCHEMA_EDIT_BUTTON = vol.Schema(
    {vol.Optional(CONF_NAME): selector.TextSelector(), **BUTTON_EDIT}
)

DATA_SCHEMA_ADD_KEYPAD = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_KEYPAD_NAME): selector.TextSelector(),
        vol.Required(CONF_ADDR): selector.TextSelector(),
    }
)

DATA_SCHEMA_ADD_CCO_DEVICE = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_CCO_NAME): selector.TextSelector(),
        vol.Required(CONF_ADDR): selector.TextSelector(),
        vol.Required(CONF_BUTTON_NUMBER, default=1): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=24, step=1, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(
            CONF_ENTITY_TYPE, default=CCO_TYPE_SWITCH
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CCO_ENTITY_TYPES,
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key="cco_entity_type",
            )
        ),
        vol.Optional(CONF_INVERTED, default=False): selector.BooleanSelector(),
        vol.Optional(CONF_AREA): selector.AreaSelector(),
    }
)

DATA_SCHEMA_EDIT_CCO_DEVICE = vol.Schema(
    {
        vol.Optional(CONF_NAME): selector.TextSelector(),
        vol.Optional(CONF_ADDR): selector.TextSelector(),
        vol.Optional(CONF_BUTTON_NUMBER): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=24, step=1, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Optional(CONF_ENTITY_TYPE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CCO_ENTITY_TYPES,
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key="cco_entity_type",
            )
        ),
        vol.Optional(CONF_INVERTED): selector.BooleanSelector(),
    }
)

DATA_SCHEMA_CONTROLLER_SETTINGS = vol.Schema(
    {
        vol.Required(
            CONF_KLS_POLL_INTERVAL, default=DEFAULT_KLS_POLL_INTERVAL
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=5,
                max=300,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        ),
        vol.Required(
            CONF_KLS_WINDOW_OFFSET, default=DEFAULT_KLS_WINDOW_OFFSET
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=16, step=1, mode=selector.NumberSelectorMode.BOX
            )
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
    "manage_cco_devices": SchemaFlowMenuStep(
        ["add_cco_device", "select_edit_cco_device", "remove_cco_device"]
    ),
    "add_cco_device": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_CCO_DEVICE, validate_user_input=validate_add_cco_device
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
        get_remove_cco_device_schema, validate_user_input=validate_remove_cco_device
    ),
    "manage_dimmers": SchemaFlowMenuStep(
        ["add_light", "select_edit_light", "remove_light"]
    ),
    "add_light": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_LIGHT, validate_user_input=validate_add_light
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
        get_remove_light_schema, validate_user_input=validate_remove_light
    ),
    "manage_keypads": SchemaFlowMenuStep(
        ["add_keypad", "select_edit_keypad", "remove_keypad"]
    ),
    "add_keypad": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_KEYPAD, validate_user_input=validate_add_keypad
    ),
    "select_edit_keypad": SchemaFlowFormStep(
        get_select_keypad_schema,
        validate_user_input=validate_select_keypad,
        next_step="edit_keypad",
    ),
    "edit_keypad": SchemaFlowMenuStep(
        ["add_button", "select_edit_button", "remove_button"]
    ),
    "remove_keypad": SchemaFlowFormStep(
        get_remove_keypad_schema, validate_user_input=validate_remove_keypad
    ),
    "add_button": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_BUTTON, validate_user_input=validate_add_button
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
        get_remove_button_schema, validate_user_input=validate_remove_button
    ),
    "controller_settings": SchemaFlowFormStep(
        DATA_SCHEMA_CONTROLLER_SETTINGS,
        suggested_values=get_controller_settings_suggested_values,
        validate_user_input=validate_controller_settings,
    ),
    "import_csv": SchemaFlowFormStep(
        vol.Schema(
            {
                vol.Required("csv_file"): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                )
            }
        ),
        validate_user_input=async_parse_csv,
        next_step="confirm_import",
    ),
    "confirm_import": SchemaFlowFormStep(
        get_confirm_import_schema, validate_user_input=validate_confirm_import
    ),
    "review_config": SchemaFlowFormStep(
        get_review_config_schema, validate_user_input=validate_review_config
    ),
}


class HomeworksConfigFlowHandler(ConfigFlow, domain=DOMAIN):
    """Config flow for Lutron Homeworks.

    Credentials (host, port, username, password) are stored in entry.data.
    Non-secrets (devices, settings) are stored in entry.options.
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user setup."""
        errors = {}
        if user_input:
            name = user_input[CONF_NAME]
            host = user_input[CONF_HOST]
            port = int(user_input[CONF_PORT])
            username = user_input.get(CONF_USERNAME)
            password = user_input.get(CONF_PASSWORD)
            controller_id = slugify(name)

            # Check for duplicates
            for entry in self._async_current_entries():
                if (
                    entry.data.get(CONF_HOST) == host
                    and entry.data.get(CONF_PORT) == port
                ):
                    return self.async_abort(reason="already_configured")

            try:
                await _try_connection(host, port, username, password)
            except SchemaFlowError as err:
                errors["base"] = str(err)
            else:
                # Credentials in entry.data (secrets)
                data = {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                }
                # Non-secrets in entry.options
                options = {
                    CONF_CONTROLLER_ID: controller_id,
                    CONF_CCO_DEVICES: [],
                    CONF_DIMMERS: [],
                    CONF_KEYPADS: [],
                    CONF_KLS_POLL_INTERVAL: DEFAULT_KLS_POLL_INTERVAL,
                    CONF_KLS_WINDOW_OFFSET: DEFAULT_KLS_WINDOW_OFFSET,
                    # Legacy keys for migration
                    CONF_CCOS: [],
                    CONF_COVERS: [],
                    CONF_LOCKS: [],
                }
                return self.async_create_entry(title=name, data=data, options=options)

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA_ADD_CONTROLLER, errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauth."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors = {}

        if user_input is not None and self._reauth_entry is not None:
            host = self._reauth_entry.data[CONF_HOST]
            port = self._reauth_entry.data[CONF_PORT]
            username = user_input.get(CONF_USERNAME)
            password = user_input.get(CONF_PASSWORD)

            try:
                await _try_connection(host, port, username, password)
            except SchemaFlowError as err:
                errors["base"] = str(err)
            else:
                # Update entry.data with new credentials
                new_data = dict(self._reauth_entry.data)
                new_data[CONF_USERNAME] = username
                new_data[CONF_PASSWORD] = password

                self.hass.config_entries.async_update_entry(
                    self._reauth_entry, data=new_data
                )
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=DATA_SCHEMA_REAUTH,
            errors=errors,
            description_placeholders={
                "host": self._reauth_entry.data.get(CONF_HOST, "")
                if self._reauth_entry
                else ""
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfigure."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry

        errors = {}
        suggested = {
            CONF_HOST: entry.data[CONF_HOST],
            CONF_PORT: entry.data[CONF_PORT],
            CONF_USERNAME: entry.data.get(CONF_USERNAME),
            CONF_PASSWORD: entry.data.get(CONF_PASSWORD),
        }

        if user_input:
            host = user_input[CONF_HOST]
            port = int(user_input[CONF_PORT])
            username = user_input.get(CONF_USERNAME)
            password = user_input.get(CONF_PASSWORD)

            # Check for duplicates (excluding self)
            for other in self._async_current_entries():
                if other.entry_id == entry.entry_id:
                    continue
                if (
                    other.data.get(CONF_HOST) == host
                    and other.data.get(CONF_PORT) == port
                ):
                    errors["base"] = "duplicated_host_port"
                    break

            if not errors:
                try:
                    await _try_connection(host, port, username, password)
                except SchemaFlowError as err:
                    errors["base"] = str(err)

            if not errors:
                new_data = {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                }
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

            suggested = {
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
            }

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                DATA_SCHEMA_RECONFIGURE, suggested
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SchemaOptionsFlowHandler:
        """Options flow handler."""
        return SchemaOptionsFlowHandler(config_entry, OPTIONS_FLOW)
