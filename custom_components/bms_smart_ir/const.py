"""Constants for BMS Smart IR — unified Broadlink + Tuya IR integration."""

from homeassistant.components.climate import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    HVACMode,
)

DOMAIN = "bms_smart_ir"

# Which backend a config entry uses.
CONF_BACKEND = "backend"
BACKEND_BROADLINK = "broadlink"
BACKEND_TUYA = "tuya"

# ======================================================================
# Shared / Broadlink (SmartIR-style) constants
# ======================================================================
CONF_NAME = "name"
CONF_DEVICE_TYPE = "device_type"
CONF_CONTROLLER = "controller"
CONF_HOST = "host"
CONF_TIMEOUT = "timeout"
CONF_MANUFACTURER = "manufacturer"
CONF_MODEL = "model"
CONF_DEVICE_CODE = "device_code"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_POWER_SENSOR = "power_sensor"
CONF_AREA = "area_id"
CONF_POWER_SCAN_SENSOR = "power_scan_sensor"
CONF_SCAN_THRESHOLD = "scan_threshold"

DEFAULT_SCAN_THRESHOLD = 50
DEFAULT_SCAN_WAIT = 8
DEFAULT_SCAN_SETTLE = 3

DEVICE_TYPE_CLIMATE = "climate"
DEVICE_TYPE_MEDIA_PLAYER = "media_player"
CONTROLLER_BROADLINK = "Broadlink"
DEFAULT_TIMEOUT = 5
DEFAULT_PORT = 80
CONF_PORT = "port"
CONF_MAC = "mac"

# ======================================================================
# Hub behaviour — the numbers that decide how the installation survives.
# Each one is here because getting it wrong caused a real outage; see the
# reliability notes in the project brief before changing any of them.
# ======================================================================
IR_GAP_MS = 180
"""Minimum spacing between two IR transmissions on one device."""

SENSOR_INTERVAL = 60
"""Sensor read interval, which is also the liveness heartbeat."""

FIRST_HEARTBEAT_DELAY = 5
"""Wait before the first heartbeat. Right after a restart Home Assistant is
busy setting everything else up; the connection is made on demand anyway."""

WATCHDOG_INTERVAL = 30
"""How often every hub is checked for 'disconnected with nobody working on it'."""

FAILURE_THRESHOLD = 3
"""Consecutive failures before a session is dropped. A single timeout is not
proof of a dead socket — dropping on the first one took whole floors down."""

BACKOFF_SECONDS = (1, 2, 5, 10, 20, 30, 60)
"""Soft backoff, capped. Not a single interval, not endless doubling."""

GRACE_SECONDS = 120
STARTUP_GRACE_SECONDS = 300
"""Entities keep their state this long before going unavailable, so Wi-Fi
micro-outages stay out of the history."""

STARTUP_STAGGER = 0.2
"""Delay between the first connect of consecutive hubs."""

MAX_PARALLEL_IO = 16
"""Blocking library calls in flight at once, across all hubs — Home Assistant's
executor pool is shared with the rest of the system."""

UPTIME_SAMPLES = 500
"""Online/offline transitions kept in memory for the panel's uptime chart."""

SIGNAL_HUB_UPDATE = "bms_smart_ir_hub_{}"

CODES_DIR = "codes"
SMARTIR_RAW_BASE = "https://raw.githubusercontent.com/smartHomeHub/SmartIR/master/codes"

# ======================================================================
# Tuya (cloud) constants
# ======================================================================
# Domain of the existing BMS Integration we reuse cloud credentials from.
BMS_DOMAIN = "bms_integration"

MANUFACTURER = "BMS Smart Home"
MODEL = "IR Air Conditioner"

# Entity kind stored per Tuya config entry
CONF_KIND = "kind"
KIND_CLIMATE = "climate"
KIND_REMOTE = "remote"

CONF_BMS_ENTRY_ID = "bms_entry_id"
CONF_INFRARED_ID = "infrared_id"
CONF_DEVICE_ID = "device_id"
CONF_CATEGORY_ID = "category_id"
CONF_CATEGORY_NAME = "category_name"

CRED_REGION = "region"
CRED_CLIENT_ID = "client_id"
CRED_CLIENT_SECRET = "client_secret"
CRED_USER_ID = "user_id"

UPDATE_INTERVAL = 300
REFRESH_AFTER_COMMAND = 4

MIN_TEMP = 16
MAX_TEMP = 30
TEMP_STEP = 1

HVAC_MODES = [
    HVACMode.OFF,
    HVACMode.COOL,
    HVACMode.HEAT,
    HVACMode.AUTO,
    HVACMode.DRY,
    HVACMode.FAN_ONLY,
]
FAN_MODES = [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH]

TUYA_HVAC_MODES = {
    "0": HVACMode.COOL,
    "1": HVACMode.HEAT,
    "2": HVACMode.AUTO,
    "3": HVACMode.FAN_ONLY,
    "4": HVACMode.DRY,
    "5": HVACMode.OFF,
}
TUYA_FAN_MODES = {"0": FAN_AUTO, "1": FAN_LOW, "2": FAN_MEDIUM, "3": FAN_HIGH}
HVAC_TO_TUYA = {
    HVACMode.COOL: 0,
    HVACMode.HEAT: 1,
    HVACMode.AUTO: 2,
    HVACMode.FAN_ONLY: 3,
    HVACMode.DRY: 4,
}
FAN_TO_TUYA = {FAN_AUTO: 0, FAN_LOW: 1, FAN_MEDIUM: 2, FAN_HIGH: 3}

DEFAULT_MODE_INT = 0
DEFAULT_TEMP = 24
DEFAULT_WIND_INT = 0

CATEGORY_NAMES = {
    "1": "TV",
    "2": "Set-top box",
    "3": "Audio",
    "4": "Box",
    "5": "Air conditioner",
    "6": "Fan",
    "7": "DVD",
    "8": "Projector",
    "9": "Camera",
    "10": "Light",
}
AC_CATEGORY_IDS = {"5"}
AC_NAME_HINTS = ("air", "condition", "konditsion", "kondit", "кондицион", "ac")
