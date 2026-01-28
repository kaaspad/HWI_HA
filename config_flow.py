"""Lutron Homeworks Series 4 and 8 config flow."""

from __future__ import annotations

from functools import partial
import logging
from typing import Any
import re
import asyncio

import voluptuous as vol
import csv
from io import StringIO
from typing import NamedTuple

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
    area_registry as ar,
    device_registry as dr,
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

from . import DEFAULT_FADE_RATE, calculate_unique_id, normalize_address
from .const import (
    CONF_ADDR,
    CONF_BUTTONS,
    CONF_CONTROLLER_ID,
    CONF_DIMMERS,
    CONF_INDEX,
    CONF_KEYPADS,
    CONF_LED,
    CONF_NUMBER,
    CONF_RATE,
    CONF_RELEASE_DELAY,
    DEFAULT_BUTTON_NAME,
    DEFAULT_KEYPAD_NAME,
    DEFAULT_LIGHT_NAME,
    DOMAIN,
    CONF_CCOS,
    CONF_RELAY_NUMBER,
    DEFAULT_CCO_NAME,
    CONF_COVERS,
    CONF_LED_ADDR,
    CONF_LED_NUMBER,
    DEFAULT_COVER_NAME,
)
from .pyhomeworks import exceptions as hw_exceptions
from .pyhomeworks.pyhomeworks import Homeworks

_LOGGER = logging.getLogger(__name__)

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

LIGHT_EDIT: VolDictType = {
    vol.Optional(CONF_RATE, default=DEFAULT_FADE_RATE): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=20,
            mode=selector.NumberSelectorMode.SLIDER,
            step=0.1,
        )
    ),
}

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


validate_addr = cv.matches_regex(r"\[(?:\d\d:){0,2}\d\d:\d\d:\d\d\]")


async def validate_add_controller(
    handler: ConfigFlow | SchemaOptionsFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate controller setup."""
    # TODO: Check not only password
    user_input[CONF_CONTROLLER_ID] = slugify(user_input[CONF_NAME])
    user_input[CONF_PORT] = int(user_input[CONF_PORT])
    try:
        handler._async_abort_entries_match(  # noqa: SLF001
            {CONF_HOST: user_input[CONF_HOST], CONF_PORT: user_input[CONF_PORT]}
        )
    except AbortFlow as err:
        raise SchemaFlowError("duplicated_host_port") from err

    try:
        handler._async_abort_entries_match(  # noqa: SLF001
            {CONF_CONTROLLER_ID: user_input[CONF_CONTROLLER_ID]}
        )
    except AbortFlow as err:
        raise SchemaFlowError("duplicated_controller_id") from err

    await _try_connection(user_input)

    return user_input


async def _try_connection(user_input: dict[str, Any]) -> None:
    """Try connecting to the controller."""

    def _try_connect(host: str, port: int) -> None:
        """Try connecting to the controller.

        Raises ConnectionError if the connection fails.
        """
        login_secret: str | None
        if CONF_PASSWORD in user_input:
            login_secret = f"{user_input[CONF_USERNAME]}, {user_input[CONF_PASSWORD]}"
        elif CONF_USERNAME in user_input:
            login_secret = user_input[CONF_USERNAME]
        else:
            login_secret = None
        _LOGGER.debug(
            "Trying to connect to %s:%s", user_input[CONF_HOST], user_input[CONF_PORT]
        )
        controller = Homeworks(host, port, lambda msg_types, values: None, login_secret)
        controller.connect()
        controller.close()

    hass = async_get_hass()
    try:
        await hass.async_add_executor_job(
            _try_connect, user_input[CONF_HOST], user_input[CONF_PORT]
        )
    except hw_exceptions.HomeworksConnectionFailed as err:
        _LOGGER.debug("Caught HomeworksConnectionFailed")
        raise SchemaFlowError("connection_error") from err
    except hw_exceptions.HomeworksInvalidCredentialsProvided as err:
        _LOGGER.debug("Caught HomeworksInvalidCredentialsProvided")
        raise SchemaFlowError("invalid_credentials") from err
    except hw_exceptions.HomeworksNoCredentialsProvided as err:
        _LOGGER.debug("Caught HomeworksNoCredentialsProvided")
        raise SchemaFlowError("credentials_needed") from err
    except Exception as err:
        _LOGGER.exception("Caught unexpected exception %s")
        raise SchemaFlowError("unknown_error") from err



def _validate_address(handler: SchemaCommonFlowHandler, addr: str, relay_number: int | None = None) -> None:
    """Validate address format."""
    try:
        # First normalize the address
        normalized = normalize_address(addr)
        # Then check if it matches expected format (3 or 4 parts)
        parts = normalized.strip('[]').split(':')
        if len(parts) not in (3, 4, 5) or not all(len(p) == 2 for p in parts):
            raise SchemaFlowError("invalid_addr")
        
        # Check for duplicates
        for item_list in (
            handler.options.get(CONF_KEYPADS, []),
            handler.options.get(CONF_DIMMERS, []),
            handler.options.get(CONF_COVERS, []),
        ):
            for item in item_list:
                if normalize_address(item[CONF_ADDR]) == normalized:
                    raise SchemaFlowError("duplicated_addr")
        if relay_number is not None:
            for item in handler.options.get(CONF_CCOS, []):
                if (normalize_address(item[CONF_ADDR]) == normalized and 
                    item[CONF_RELAY_NUMBER] == relay_number):
                    raise SchemaFlowError("duplicate_cco")
    except ValueError:
        raise SchemaFlowError("invalid_addr")


def _validate_button_number(handler: SchemaCommonFlowHandler, number: int) -> None:
    """Validate button number."""
    keypad = handler.flow_state["_idx"]
    buttons: list[dict[str, Any]] = handler.options[CONF_KEYPADS][keypad][CONF_BUTTONS]

    for button in buttons:
        if button[CONF_NUMBER] == number:
            raise SchemaFlowError("duplicated_number")


async def validate_add_button(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate button input."""
    user_input[CONF_NUMBER] = int(user_input[CONF_NUMBER])
    _validate_button_number(handler, user_input[CONF_NUMBER])

    # Standard behavior is to merge the result with the options.
    # In this case, we want to add a sub-item so we update the options directly.
    keypad = handler.flow_state["_idx"]
    buttons: list[dict[str, Any]] = handler.options[CONF_KEYPADS][keypad][CONF_BUTTONS]
    buttons.append(user_input)
    return {}


async def validate_add_keypad(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate keypad or light input."""
    _validate_address(handler, user_input[CONF_ADDR])
    
    # Normalize the address before saving
    user_input[CONF_ADDR] = normalize_address(user_input[CONF_ADDR])
    
    items = handler.options.setdefault(CONF_KEYPADS, [])
    #items.append(user_input)
    #items = handler.options[CONF_KEYPADS]
    items.append(user_input | {CONF_BUTTONS: []})
    return {}


async def validate_add_light(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate light input."""
    _validate_address(handler, user_input[CONF_ADDR])
    
    # Normalize the address before saving
    user_input[CONF_ADDR] = normalize_address(user_input[CONF_ADDR])
    
    #items = handler.options.setdefault(CONF_DIMMERS, [])
    items = handler.options[CONF_DIMMERS]
    items.append(user_input)
    return {}


async def get_select_button_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a button."""
    keypad = handler.flow_state["_idx"]
    buttons: list[dict[str, Any]] = handler.options[CONF_KEYPADS][keypad][CONF_BUTTONS]

    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_NUMBER]})"
                    for index, config in enumerate(buttons)
                },
            )
        }
    )


async def get_select_keypad_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a keypad."""
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_ADDR]})"
                    for index, config in enumerate(handler.options[CONF_KEYPADS])
                },
            )
        }
    )


async def get_select_light_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a light."""
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_ADDR]})"
                    for index, config in enumerate(handler.options[CONF_DIMMERS])
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


async def validate_select_keypad_light(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store keypad or light index in flow state."""
    handler.flow_state["_idx"] = int(user_input[CONF_INDEX])
    return {}


async def get_edit_button_suggested_values(
    handler: SchemaCommonFlowHandler,
) -> dict[str, Any]:
    """Return suggested values for button editing."""
    keypad_idx: int = handler.flow_state["_idx"]
    button_idx: int = handler.flow_state["_button_idx"]
    return dict(handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS][button_idx])


async def get_edit_light_suggested_values(
    handler: SchemaCommonFlowHandler,
) -> dict[str, Any]:
    """Return suggested values for light editing."""
    idx: int = handler.flow_state["_idx"]
    return dict(handler.options[CONF_DIMMERS][idx])


async def validate_button_edit(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Update edited keypad or light."""
    # Standard behavior is to merge the result with the options.
    # In this case, we want to add a sub-item so we update the options directly.
    keypad_idx: int = handler.flow_state["_idx"]
    button_idx: int = handler.flow_state["_button_idx"]
    buttons: list[dict] = handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS]
    buttons[button_idx].update(user_input)
    return {}


async def validate_light_edit(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Update edited keypad or light."""
    # Standard behavior is to merge the result with the options.
    # In this case, we want to add a sub-item so we update the options directly.
    idx: int = handler.flow_state["_idx"]
    handler.options[CONF_DIMMERS][idx].update(user_input)
    return {}


async def get_remove_button_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for button removal."""
    keypad_idx: int = handler.flow_state["_idx"]
    buttons: list[dict] = handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS]
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): cv.multi_select(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_NUMBER]})"
                    for index, config in enumerate(buttons)
                },
            )
        }
    )


async def get_remove_keypad_light_schema(
    handler: SchemaCommonFlowHandler, *, key: str
) -> vol.Schema:
    """Return schema for keypad or light removal."""
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): cv.multi_select(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_ADDR]})"
                    for index, config in enumerate(handler.options[key])
                },
            )
        }
    )


async def validate_remove_button(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate remove keypad or light."""
    removed_indexes: set[str] = set(user_input[CONF_INDEX])

    # Standard behavior is to merge the result with the options.
    # In this case, we want to remove sub-items so we update the options directly.
    entity_registry = er.async_get(handler.parent_handler.hass)
    keypad_idx: int = handler.flow_state["_idx"]
    keypad: dict = handler.options[CONF_KEYPADS][keypad_idx]
    items: list[dict[str, Any]] = []
    item: dict[str, Any]
    for index, item in enumerate(keypad[CONF_BUTTONS]):
        if str(index) not in removed_indexes:
            items.append(item)
        button_number = keypad[CONF_BUTTONS][index][CONF_NUMBER]
        for domain in (BINARY_SENSOR_DOMAIN, BUTTON_DOMAIN):
            if entity_id := entity_registry.async_get_entity_id(
                domain,
                DOMAIN,
                calculate_unique_id(
                    handler.options[CONF_CONTROLLER_ID],
                    keypad[CONF_ADDR],
                    button_number,
                ),
            ):
                entity_registry.async_remove(entity_id)
    keypad[CONF_BUTTONS] = items
    return {}


async def validate_remove_keypad_light(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any], *, key: str
) -> dict[str, Any]:
    """Validate remove keypad or light."""
    removed_indexes: set[str] = set(user_input[CONF_INDEX])

    # Standard behavior is to merge the result with the options.
    # In this case, we want to remove sub-items so we update the options directly.
    entity_registry = er.async_get(handler.parent_handler.hass)
    items: list[dict[str, Any]] = []
    item: dict[str, Any]
    for index, item in enumerate(handler.options[key]):
        if str(index) not in removed_indexes:
            items.append(item)
        elif key != CONF_DIMMERS:
            continue
        if entity_id := entity_registry.async_get_entity_id(
            LIGHT_DOMAIN,
            DOMAIN,
            calculate_unique_id(
                handler.options[CONF_CONTROLLER_ID], item[CONF_ADDR], 0
            ),
        ):
            entity_registry.async_remove(entity_id)
    handler.options[key] = items
    return {}


async def validate_remove_cco(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any], *, key: str
) -> dict[str, Any]:
    """Validate remove CCO."""
    removed_indexes: set[str] = set(user_input[CONF_INDEX])

    # Standard behavior is to merge the result with the options.
    # In this case, we want to remove sub-items so we update the options directly.
    entity_registry = er.async_get(handler.parent_handler.hass)
    items: list[dict[str, Any]] = []
    item: dict[str, Any]
    for index, item in enumerate(handler.options[key]):
        if str(index) not in removed_indexes:
            items.append(item)
            continue
        if entity_id := entity_registry.async_get_entity_id(
            "switch",
            DOMAIN,
            calculate_unique_id(
                handler.options[CONF_CONTROLLER_ID],
                item[CONF_ADDR],
                item[CONF_RELAY_NUMBER]
            ),
        ):
            entity_registry.async_remove(entity_id)
    handler.options[key] = items
    return {}


async def validate_remove_cover(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any], *, key: str
) -> dict[str, Any]:
    """Validate remove cover."""
    removed_indexes: set[str] = set(user_input[CONF_INDEX])

    # Standard behavior is to merge the result with the options.
    # In this case, we want to remove sub-items so we update the options directly.
    entity_registry = er.async_get(handler.parent_handler.hass)
    items: list[dict[str, Any]] = []
    item: dict[str, Any]
    for index, item in enumerate(handler.options[key]):
        if str(index) not in removed_indexes:
            items.append(item)
            continue
        if entity_id := entity_registry.async_get_entity_id(
            "cover",
            DOMAIN,
            calculate_unique_id(
                handler.options[CONF_CONTROLLER_ID], item[CONF_ADDR], 0
            ),
        ):
            entity_registry.async_remove(entity_id)
    handler.options[key] = items
    return {}


DATA_SCHEMA_ADD_CONTROLLER = vol.Schema(
    {
        vol.Required(
            CONF_NAME, description={"suggested_value": "Lutron Homeworks"}
        ): selector.TextSelector(),
        **CONTROLLER_EDIT,
    }
)
DATA_SCHEMA_EDIT_CONTROLLER = vol.Schema(CONTROLLER_EDIT)
DATA_SCHEMA_ADD_LIGHT = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_LIGHT_NAME): TextSelector(),
        vol.Required(CONF_ADDR): TextSelector(),
        **LIGHT_EDIT,
    }
)
DATA_SCHEMA_ADD_KEYPAD = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_KEYPAD_NAME): TextSelector(),
        vol.Required(CONF_ADDR): TextSelector(),
    }
)
DATA_SCHEMA_ADD_BUTTON = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_BUTTON_NAME): TextSelector(),
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
DATA_SCHEMA_EDIT_BUTTON = vol.Schema(BUTTON_EDIT)
DATA_SCHEMA_EDIT_LIGHT = vol.Schema(LIGHT_EDIT)
DATA_SCHEMA_ADD_CCO = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_CCO_NAME): TextSelector(),
        vol.Required(CONF_ADDR): TextSelector(),
        vol.Required(CONF_RELAY_NUMBER): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=8,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
            ),
        ),
    }
)
DATA_SCHEMA_ADD_COVER = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_COVER_NAME): TextSelector(),
        vol.Required(CONF_ADDR): TextSelector(),
    }
)


async def validate_add_cco(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate CCO input."""
    _validate_address(handler, user_input[CONF_ADDR])
    
    # Normalize the address before saving
    user_input[CONF_ADDR] = normalize_address(user_input[CONF_ADDR])
    
    items = handler.options.setdefault(CONF_CCOS, [])
    items.append(user_input)
    return {}


async def validate_add_cover(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate cover input."""
    _validate_address(handler, user_input[CONF_ADDR])
    
    # Normalize the address before saving
    user_input[CONF_ADDR] = normalize_address(user_input[CONF_ADDR])
    
    items = handler.options.setdefault(CONF_COVERS, [])
    items.append(user_input)
    return {}


async def validate_cover_edit(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Update edited cover."""
    # Standard behavior is to merge the result with the options.
    # In this case, we want to add a sub-item so we update the options directly.
    idx: int = handler.flow_state["_idx"]
    handler.options[CONF_COVERS][idx].update(user_input)
    return {}


async def get_select_cover_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a cover."""
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_ADDR]})"
                    for index, config in enumerate(handler.options.get(CONF_COVERS, []))
                },
            )
        }
    )


async def get_edit_cover_suggested_values(
    handler: SchemaCommonFlowHandler,
) -> dict[str, Any]:
    """Return suggested values for cover editing."""
    idx: int = handler.flow_state["_idx"]
    return dict(handler.options[CONF_COVERS][idx])


async def get_select_cco_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a CCO."""
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_ADDR]})"
                    for index, config in enumerate(handler.options.get(CONF_CCOS, []))
                },
            )
        }
    )


class DeviceImport(NamedTuple):
    """Represents a device import from CSV."""
    device_type: str
    address: str
    relay: int | None
    name: str

async def async_parse_csv(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Parse CSV content and store in flow state."""
    content = user_input["csv_file"]
    _LOGGER.debug("Parsing CSV content: %s", content)
    
    f = StringIO(content)
    reader = csv.DictReader(f)
    
    devices = []
    try:
        for row in reader:
            _LOGGER.debug("Processing row: %s", row)
            device_type = row['device_type'].strip().upper()
            
            # Handle different device types
            if device_type == "CCO":
                relay = int(row['relay']) if row.get('relay') else None
                device = DeviceImport(
                    device_type=device_type,
                    address=normalize_address(row['address'].strip()),
                    relay=relay,
                    name=row['name'].strip()
                )
            elif device_type in ("LIGHT", "COVER"):
                device = DeviceImport(
                    device_type=device_type,
                    address=normalize_address(row['address'].strip()),
                    relay=None,  # Not used for lights or covers
                    name=row['name'].strip()
                )
            else:
                _LOGGER.warning("Unknown device type: %s", device_type)
                continue
                
            _LOGGER.debug("Created device: %s", device)
            devices.append(device)
    except Exception as err:
        _LOGGER.exception("Error processing CSV row")
        raise SchemaFlowError("invalid_csv") from err
    
    _LOGGER.debug("Total devices found: %s", len(devices))
    handler.flow_state["import_devices"] = devices
    return {}

async def get_confirm_import_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for confirming device imports."""
    devices = handler.flow_state.get("import_devices", [])
    _LOGGER.debug("Getting confirm schema for devices: %s", devices)
    
    # Get entity registry to check for existing entities
    entity_registry = er.async_get(handler.parent_handler.hass)
    
    # Create selection dictionary for new devices only
    selections = {}
    existing_devices = []
    
    # Get current options to check for duplicates
    current_options = handler.options
    
    for idx, dev in enumerate(devices):
        # Create base description
        desc = f"{dev.device_type}: {dev.name} ({dev.address})"
        if dev.relay:
            desc += f" Relay {dev.relay}"
            
        # Check if entity already exists
        entity_id = None
        if dev.device_type == "LIGHT":
            entity_id = f"light.{calculate_unique_id(handler.options[CONF_CONTROLLER_ID], dev.address, 0)}"
            # Also check for duplicate address
            if any(d[CONF_ADDR] == dev.address for d in current_options.get(CONF_DIMMERS, [])):
                existing_devices.append(f"{desc} (Address already exists)")
                continue
        elif dev.device_type == "CCO":
            if dev.relay is None:
                continue
            entity_id = f"switch.{calculate_unique_id(handler.options[CONF_CONTROLLER_ID], dev.address, dev.relay)}"
            # Check for duplicate address and relay combination
            if any(d[CONF_ADDR] == dev.address and d[CONF_RELAY_NUMBER] == dev.relay 
                  for d in current_options.get(CONF_CCOS, [])):
                existing_devices.append(f"{desc} (Address/Relay already exists)")
                continue
        elif dev.device_type == "COVER":
            # Handle name deduplication for covers
            name = dev.name.strip()
            if not name:
                name = DEFAULT_COVER_NAME
            else:
                # If the name is already in the format "name name", remove the duplication
                parts = name.split()
                if len(parts) == 2 and parts[0] == parts[1]:
                    name = parts[0]
                
            new_options.setdefault(CONF_COVERS, []).append({
                CONF_ADDR: dev.address,
                CONF_NAME: name,
            })
            entity_id = f"cover.{handler.options[CONF_CONTROLLER_ID]}.{dev.address}"
            # Check for duplicate address
            if any(d[CONF_ADDR] == dev.address for d in current_options.get(CONF_COVERS, [])):
                existing_devices.append(f"{desc} (Address already exists)")
                continue
        
        if entity_id and entity_registry.async_get(entity_id):
            existing_devices.append(f"{desc} (Entity already exists)")
            continue
            
        selections[str(idx)] = desc
    
    description = []
    if existing_devices:
        description.append(
            "The following devices already exist and will be skipped:\n"
            + "\n".join(f"• {dev}" for dev in existing_devices)
        )
    
    if selections:
        if description:
            description.append("\nSelect new devices to import:")
        else:
            description.append("Select devices to import:")
    else:
        if not description:
            description.append("No new devices to import.")
    
    handler.flow_state["description"] = "\n".join(description)
    
    return vol.Schema({
        vol.Optional("devices", default=list(selections.keys())): cv.multi_select(selections)
    })

async def validate_confirm_import(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate and process the selected devices for import."""
    devices = handler.flow_state.get("import_devices", [])
    selected_indices = user_input.get("devices", [])
    
    # Prepare new options
    new_options = dict(handler.options)
    
    for idx in selected_indices:
        device = devices[int(idx)]
        
        # Add new device
        if device.device_type == "LIGHT":
            new_options.setdefault(CONF_DIMMERS, []).append({
                CONF_ADDR: device.address,
                CONF_NAME: device.name,
                CONF_RATE: DEFAULT_FADE_RATE,
            })
            
        elif device.device_type == "CCO":
            if device.relay is None:
                continue
            new_options.setdefault(CONF_CCOS, []).append({
                CONF_ADDR: device.address,
                CONF_NAME: device.name,
                CONF_RELAY_NUMBER: device.relay,
            })
            
        elif device.device_type == "COVER":
            # Handle name deduplication for covers
            name = device.name.strip()
            if not name:
                name = DEFAULT_COVER_NAME
            else:
                # If the name is already in the format "name name", remove the duplication
                parts = name.split()
                if len(parts) == 2 and parts[0] == parts[1]:
                    name = parts[0]
                
            new_options.setdefault(CONF_COVERS, []).append({
                CONF_ADDR: device.address,
                CONF_NAME: name,
            })
    
    return new_options

OPTIONS_FLOW = {
    "init": SchemaFlowMenuStep(
        [
            "import_csv",
            "add_keypad",
            "select_edit_keypad",
            "remove_keypad",
            "add_light",
            "select_edit_light",
            "remove_light",
            "add_cco",
            "remove_cco",
            "select_edit_cco",
            "add_cover",
            "remove_cover",
            "select_edit_cover",
        ]
    ),
    "import_csv": SchemaFlowFormStep(
        vol.Schema({
            vol.Required("csv_file"): selector.TextSelector(
                selector.TextSelectorConfig(
                    multiline=True,
                    type=selector.TextSelectorType.TEXT
                )
            )
        }),
        validate_user_input=async_parse_csv,
        next_step="confirm_import"
    ),
    "confirm_import": SchemaFlowFormStep(
        get_confirm_import_schema,
        validate_user_input=validate_confirm_import,
    ),
    "add_keypad": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_KEYPAD,
        suggested_values=None,
        validate_user_input=validate_add_keypad,
    ),
    "select_edit_keypad": SchemaFlowFormStep(
        get_select_keypad_schema,
        suggested_values=None,
        validate_user_input=validate_select_keypad_light,
        next_step="edit_keypad",
    ),
    "edit_keypad": SchemaFlowMenuStep(
        [
            "add_button",
            "select_edit_button",
            "remove_button",
        ]
    ),
    "add_button": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_BUTTON,
        suggested_values=None,
        validate_user_input=validate_add_button,
    ),
    "select_edit_button": SchemaFlowFormStep(
        get_select_button_schema,
        suggested_values=None,
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
        suggested_values=None,
        validate_user_input=validate_remove_button,
    ),
    "remove_keypad": SchemaFlowFormStep(
        partial(get_remove_keypad_light_schema, key=CONF_KEYPADS),
        suggested_values=None,
        validate_user_input=partial(validate_remove_keypad_light, key=CONF_KEYPADS),
    ),
    "add_light": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_LIGHT,
        suggested_values=None,
        validate_user_input=validate_add_light,
    ),
    "select_edit_light": SchemaFlowFormStep(
        get_select_light_schema,
        suggested_values=None,
        validate_user_input=validate_select_keypad_light,
        next_step="edit_light",
    ),
    "edit_light": SchemaFlowFormStep(
        DATA_SCHEMA_EDIT_LIGHT,
        suggested_values=get_edit_light_suggested_values,
        validate_user_input=validate_light_edit,
    ),
    "remove_light": SchemaFlowFormStep(
        partial(get_remove_keypad_light_schema, key=CONF_DIMMERS),
        suggested_values=None,
        validate_user_input=partial(validate_remove_keypad_light, key=CONF_DIMMERS),
    ),
    "add_cco": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_CCO,
        suggested_values=None,
        validate_user_input=validate_add_cco,
    ),
    "remove_cco": SchemaFlowFormStep(
        partial(get_remove_keypad_light_schema, key=CONF_CCOS),
        suggested_values=None,
        validate_user_input=partial(validate_remove_cco, key=CONF_CCOS),
    ),
    "select_edit_cco": SchemaFlowFormStep(
        get_select_cco_schema,
        suggested_values=None,
        validate_user_input=validate_select_keypad_light,
        next_step="edit_cco",
    ),
    "add_cover": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_COVER,
        suggested_values=None,
        validate_user_input=validate_add_cover,
    ),
    "remove_cover": SchemaFlowFormStep(
        partial(get_remove_keypad_light_schema, key=CONF_COVERS),
        suggested_values=None,
        validate_user_input=partial(validate_remove_cover, key=CONF_COVERS),
    ),
    "select_edit_cover": SchemaFlowFormStep(
        get_select_cover_schema,
        suggested_values=None,
        validate_user_input=validate_select_keypad_light,
        next_step="edit_cover",
    ),
    "edit_cover": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_COVER,
        suggested_values=get_edit_cover_suggested_values,
        validate_user_input=validate_cover_edit,
    ),
}


class HomeworksConfigFlowHandler(ConfigFlow, domain=DOMAIN):
    """Config flow for Lutron Homeworks."""

    async def _validate_edit_controller(
        self, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate controller setup."""
        # TODO: Check not only password
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
        }

        if user_input:
            suggested_values = {
                CONF_HOST: user_input[CONF_HOST],
                CONF_PORT: user_input[CONF_PORT],
            }
            try:
                await self._validate_edit_controller(user_input)
            except SchemaFlowError as err:
                errors["base"] = str(err)
            else:
                new_options = entry.options | {
                    CONF_HOST: user_input[CONF_HOST],
                    CONF_PORT: user_input[CONF_PORT],
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
                    CONF_CCOS: [],
                    CONF_COVERS: [],
                }
                return self.async_create_entry(title=name, data={}, options=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA_ADD_CONTROLLER,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SchemaOptionsFlowHandler:
        """Options flow handler for Lutron Homeworks."""
        return SchemaOptionsFlowHandler(config_entry, OPTIONS_FLOW)
