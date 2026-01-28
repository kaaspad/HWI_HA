"""Constants for the Lutron Homeworks integration."""

from __future__ import annotations

DOMAIN = "homeworks"

CONF_ADDR = "addr"
CONF_BUTTONS = "buttons"
CONF_CONTROLLER_ID = "controller_id"
CONF_DIMMERS = "dimmers"
CONF_INDEX = "index"
CONF_KEYPADS = "keypads"
CONF_LED = "led"
CONF_NUMBER = "number"
CONF_RATE = "rate"
CONF_RELEASE_DELAY = "release_delay"
CONF_CCOS = "ccos"  # For CCO relay configuration
CONF_RELAY_NUMBER = "relay_number"  # For CCO relay number
CONF_LED_ADDR = "led_addr"        # For CCO relay LED keypad address
CONF_LED_NUMBER = "led_number"    # For CCO relay LED number
CONF_COVERS = "covers"            # For cover configuration

DEFAULT_BUTTON_NAME = "Homeworks button"
DEFAULT_KEYPAD_NAME = "Homeworks keypad"
DEFAULT_LIGHT_NAME = "Homeworks light"
DEFAULT_CCO_NAME = "Homeworks CCO"  # Default name for CCO relays
DEFAULT_COVER_NAME = "Homeworks Cover"  # Default name for covers
