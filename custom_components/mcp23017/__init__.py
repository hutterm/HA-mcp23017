"""Support for I2C MCP23017 chip."""

import asyncio
import functools
import logging
from collections import defaultdict
from time import monotonic
from types import MappingProxyType
from typing import Any

import smbus2
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import callback
from homeassistant.helpers import device_registry, entity_registry as er
import homeassistant.helpers.config_validation as cv
from homeassistant.components import persistent_notification

from .const import (
    CONF_FLOW_PLATFORM,
    CONF_IMPORT_SUBENTRIES,
    CONF_I2C_ADDRESS,
    CONF_I2C_BUS,
    CONF_INVERT_LOGIC,
    CONF_FLOW_PIN_NAME,
    CONF_FLOW_PIN_NUMBER,
    CONF_HW_SYNC,
    CONF_PIN_CONFIGS,
    CONF_MOMENTARY,
    CONF_PULSE_TIME,
    CONF_READOUT_ENABLED,
    CONF_PULL_MODE,
    CONF_SCAN_RATE,
    SERVICE_RUN_PATTERN,
    SERVICE_STOP_PATTERN,
    ATTR_PATTERN_DURATION_MS,
    ATTR_PATTERN_NAME,
    ATTR_PATTERN_PINS,
    ATTR_PATTERN_REPEAT_COUNT,
    ATTR_PATTERN_REPEAT_FOR_SECONDS,
    ATTR_PATTERN_STATE,
    ATTR_PATTERN_STEPS,
    DEFAULT_HW_SYNC,
    DEFAULT_I2C_BUS,
    DEFAULT_I2C_LOCKS_KEY,
    DEFAULT_INVERT_LOGIC,
    DEFAULT_MOMENTARY,
    DEFAULT_PULSE_TIME,
    DEFAULT_READOUT_ENABLED,
    DEFAULT_SCAN_RATE,
    DOMAIN,
    PULL_MODE_UP,
    PULL_MODE_NONE,
    SUBENTRY_TYPE_PIN,
)
from .i2c_lock import get_i2c_bus_lock

# MCP23017 Register Map (IOCON.BANK = 1, MCP23008-compatible)

REGISTER_MAP = {
    "IODIRA": 0x00,
    "IODIRB": 0x10,
    "IPOLA": 0x01,
    "IPOLB": 0x11,
    "GPINTENA": 0x02,
    "GPINTENB": 0x12,
    "DEFVALA": 0x03,
    "DEFVALB": 0x13,
    "INTCONA": 0x04,
    "INTCONB": 0x14,
    "IOCONA": 0x05,
    "IOCONB": 0x15,
    "GPPUA": 0x06,
    "GPPUB": 0x16,
    "INTFA": 0x07,
    "INTFB": 0x17,
    "INTCAPA": 0x08,
    "INTCAPB": 0x18,
    "GPIOA": 0x09,
    "GPIOB": 0x19,
    "OLATA": 0x0a,
    "OLATB": 0x1a,
}


# Register address used to toggle IOCON.BANK to 1 (only mapped when BANK is 0)
IOCON_REMAP = 0x0b

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["binary_sensor", "switch", "number"]

MCP23017_DATA_LOCK = asyncio.Lock()
SCAN_RATE_DEFAULT = DEFAULT_SCAN_RATE

I2C_LOCKS_KEY = DEFAULT_I2C_LOCKS_KEY

_PATTERN_STATE_ON = {"on", "true", "1"}
_PATTERN_STATE_OFF = {"off", "false", "0"}


class SetupEntryStatus:
    """Class registering the number of outstanding async_setup_entry calls."""
    def __init__(self):
        """Initialize call counter."""
        self.number = 0
    def __enter__(self):
        """Increment call counter (with statement)."""
        self.number +=1
    def __exit__(self, exc_type, exc_value, exc_tb):
        """Decrement call counter (with statement)."""
        self.number -=1
    def busy(self):
        """Return True when there is at least one outstanding call"""
        return self.number != 0

setup_entry_status = SetupEntryStatus()


def _chip_unique_id(i2c_bus: int, i2c_address: int) -> str:
    return f"{DOMAIN}.{i2c_bus}.{i2c_address}"


def _chip_title(i2c_bus: int, i2c_address: int) -> str:
    return f"Bus: {i2c_bus}, address: 0x{i2c_address:02x}"


def _pin_subentry_unique_id(platform: str, pin_number: int) -> str:
    return f"{platform}:{pin_number}"


def _pin_subentry_title(data: dict) -> str:
    return (
        f"{data[CONF_FLOW_PIN_NAME]} "
        f"({data[CONF_FLOW_PLATFORM]}, pin {data[CONF_FLOW_PIN_NUMBER]})"
    )


def _normalize_scan_rate(scan_rate) -> float:
    try:
        value = float(scan_rate)
    except (TypeError, ValueError):
        value = DEFAULT_SCAN_RATE
    return max(0.01, value)


def _normalize_pull_mode(pull_mode):
    if isinstance(pull_mode, str):
        return PULL_MODE_NONE if pull_mode.lower() == PULL_MODE_NONE else PULL_MODE_UP
    return PULL_MODE_UP


def _normalize_pin_config(pin_config: dict) -> dict | None:
    try:
        pin_number = int(pin_config[CONF_FLOW_PIN_NUMBER])
        platform = str(pin_config[CONF_FLOW_PLATFORM])
    except (KeyError, TypeError, ValueError):
        return None

    if platform not in ("binary_sensor", "switch"):
        return None

    pin_name = str(pin_config.get(CONF_FLOW_PIN_NAME, f"pin {pin_number}")).strip()
    if not pin_name:
        pin_name = f"pin {pin_number}"

    normalized: dict = {
        CONF_FLOW_PLATFORM: platform,
        CONF_FLOW_PIN_NUMBER: pin_number,
        CONF_FLOW_PIN_NAME: pin_name,
        CONF_INVERT_LOGIC: bool(pin_config.get(CONF_INVERT_LOGIC, DEFAULT_INVERT_LOGIC)),
    }

    if platform == "binary_sensor":
        normalized[CONF_PULL_MODE] = _normalize_pull_mode(pin_config.get(CONF_PULL_MODE))
    else:
        normalized[CONF_HW_SYNC] = bool(pin_config.get(CONF_HW_SYNC, DEFAULT_HW_SYNC))
        normalized[CONF_MOMENTARY] = bool(pin_config.get(CONF_MOMENTARY, DEFAULT_MOMENTARY))
        normalized[CONF_PULSE_TIME] = max(
            0,
            int(pin_config.get(CONF_PULSE_TIME, DEFAULT_PULSE_TIME)),
        )

    return normalized


def _pin_configs_from_entry(config_entry: ConfigEntry) -> list[dict]:
    pin_configs_by_pin: dict[int, dict] = {}

    for pin_config in config_entry.data.get(CONF_PIN_CONFIGS, []):
        normalized = _normalize_pin_config(dict(pin_config))
        if normalized is not None:
            pin_configs_by_pin[int(normalized[CONF_FLOW_PIN_NUMBER])] = normalized

    for subentry_data in config_entry.data.get(CONF_IMPORT_SUBENTRIES, []):
        normalized = _normalize_pin_config(dict(subentry_data))
        if normalized is not None:
            pin_configs_by_pin[int(normalized[CONF_FLOW_PIN_NUMBER])] = normalized

    for subentry in config_entry.subentries.values():
        normalized = _normalize_pin_config(dict(subentry.data))
        if normalized is not None:
            pin_configs_by_pin[int(normalized[CONF_FLOW_PIN_NUMBER])] = normalized

    return [pin_configs_by_pin[pin] for pin in sorted(pin_configs_by_pin)]


async def async_migrate_entry_pin_configs(hass, config_entry: ConfigEntry) -> None:
    """Collapse pin subentries into parent entry pin_configs data."""
    pin_configs = _pin_configs_from_entry(config_entry)

    new_data = {
        key: value
        for key, value in config_entry.data.items()
        if key != CONF_IMPORT_SUBENTRIES and key != CONF_PIN_CONFIGS
    }
    if pin_configs:
        new_data[CONF_PIN_CONFIGS] = pin_configs

    if new_data != dict(config_entry.data):
        hass.config_entries.async_update_entry(config_entry, data=new_data)

    if config_entry.subentries:
        for subentry_id in list(config_entry.subentries):
            hass.config_entries.async_remove_subentry(config_entry, subentry_id)


def _normalize_pattern_state(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        state = value.lower()
        if state in _PATTERN_STATE_ON:
            return True
        if state in _PATTERN_STATE_OFF:
            return False
    raise HomeAssistantError(
        f"Invalid pattern step state: {value}. Use one of on/off/true/false/1/0."
    )


def _validate_pattern_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise vol.Invalid("Pattern name cannot be empty")
    return name


def _normalize_pattern_step(step: dict[str, Any]) -> tuple[bool, int]:
    if not isinstance(step, dict):
        raise vol.Invalid("Each step must be an object")

    if ATTR_PATTERN_STATE in step or ATTR_PATTERN_DURATION_MS in step:
        if ATTR_PATTERN_STATE not in step or ATTR_PATTERN_DURATION_MS not in step:
            raise vol.Invalid(
                f"Legacy step format requires both {ATTR_PATTERN_STATE} and "
                f"{ATTR_PATTERN_DURATION_MS}"
            )
        state = _normalize_pattern_state(step[ATTR_PATTERN_STATE])
        duration_ms = vol.All(vol.Coerce(int), vol.Range(min=1))(
            step[ATTR_PATTERN_DURATION_MS]
        )
        return (state, duration_ms)

    if len(step) != 1:
        raise vol.Invalid(
            "Step must contain exactly one key, e.g. {'on': 200} or {'off': 300}"
        )

    state_key, duration_value = next(iter(step.items()))
    state = _normalize_pattern_state(state_key)
    duration_ms = vol.All(vol.Coerce(int), vol.Range(min=1))(duration_value)
    return (state, duration_ms)


def _normalize_pattern_steps(steps: list[dict[str, Any]]) -> list[tuple[bool, int]]:
    return [_normalize_pattern_step(step) for step in steps]

RUN_PATTERN_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_I2C_BUS): vol.All(vol.Coerce(int), vol.Range(min=0, max=9)),
        vol.Required(CONF_I2C_ADDRESS): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=127)
        ),
        vol.Required(ATTR_PATTERN_NAME): _validate_pattern_name,
        vol.Required(ATTR_PATTERN_PINS): vol.All(
            cv.ensure_list,
            [vol.All(vol.Coerce(int), vol.Range(min=0, max=15))],
            vol.Length(min=1),
        ),
        vol.Required(ATTR_PATTERN_STEPS): vol.All(
            cv.ensure_list,
            [dict],
            vol.Length(min=1),
            _normalize_pattern_steps,
        ),
        vol.Optional(ATTR_PATTERN_REPEAT_COUNT): vol.All(
            vol.Coerce(int), vol.Range(min=1)
        ),
        vol.Optional(ATTR_PATTERN_REPEAT_FOR_SECONDS): vol.All(
            vol.Coerce(float), vol.Range(min=0.001)
        ),
    }
)

STOP_PATTERN_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_I2C_BUS): vol.All(vol.Coerce(int), vol.Range(min=0, max=9)),
        vol.Required(CONF_I2C_ADDRESS): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=127)
        ),
        vol.Required(ATTR_PATTERN_NAME): _validate_pattern_name,
    }
)


def _legacy_subentry_data(config_entry: ConfigEntry) -> dict:
    i2c_bus = int(config_entry.data.get(CONF_I2C_BUS, DEFAULT_I2C_BUS))
    i2c_address = int(config_entry.data[CONF_I2C_ADDRESS])
    pin_number = int(config_entry.data[CONF_FLOW_PIN_NUMBER])
    platform = str(config_entry.data.get(CONF_FLOW_PLATFORM, "binary_sensor"))
    pin_name = config_entry.data.get(
        CONF_FLOW_PIN_NAME, f"pin {i2c_bus}:0x{i2c_address:02x}:{pin_number}"
    )

    data = {
        CONF_FLOW_PLATFORM: platform,
        CONF_FLOW_PIN_NUMBER: pin_number,
        CONF_FLOW_PIN_NAME: pin_name,
        CONF_INVERT_LOGIC: bool(
            config_entry.options.get(
                CONF_INVERT_LOGIC,
                config_entry.data.get(CONF_INVERT_LOGIC, DEFAULT_INVERT_LOGIC),
            )
        ),
    }
    if platform == "binary_sensor":
        data[CONF_PULL_MODE] = _normalize_pull_mode(
            config_entry.options.get(
                CONF_PULL_MODE, config_entry.data.get(CONF_PULL_MODE, PULL_MODE_UP)
            )
        )
    else:
        data[CONF_HW_SYNC] = bool(
            config_entry.options.get(
                CONF_HW_SYNC,
                config_entry.data.get(CONF_HW_SYNC, DEFAULT_HW_SYNC),
            )
        )
        data[CONF_MOMENTARY] = bool(
            config_entry.options.get(
                CONF_MOMENTARY,
                config_entry.data.get(CONF_MOMENTARY, DEFAULT_MOMENTARY),
            )
        )
        data[CONF_PULSE_TIME] = max(
            0,
            int(
                config_entry.options.get(
                    CONF_PULSE_TIME,
                    config_entry.data.get(CONF_PULSE_TIME, DEFAULT_PULSE_TIME),
                )
            ),
        )
    return data


@callback
def _ensure_subentry(
    hass,
    config_entry: ConfigEntry,
    subentry_data: dict,
) -> ConfigSubentry:
    pin_number = int(subentry_data[CONF_FLOW_PIN_NUMBER])
    subentry_unique_id = _pin_subentry_unique_id(
        subentry_data[CONF_FLOW_PLATFORM],
        pin_number,
    )
    for existing_subentry in config_entry.subentries.values():
        if int(existing_subentry.data.get(CONF_FLOW_PIN_NUMBER, -1)) == pin_number:
            return existing_subentry
        if existing_subentry.unique_id == subentry_unique_id:
            return existing_subentry

    subentry = ConfigSubentry(
        data=MappingProxyType(subentry_data),
        subentry_type=SUBENTRY_TYPE_PIN,
        title=_pin_subentry_title(subentry_data),
        unique_id=subentry_unique_id,
    )
    hass.config_entries.async_add_subentry(config_entry, subentry)
    return subentry


@callback
def _migrate_entities_to_subentry(
    hass,
    source_entry: ConfigEntry,
    target_entry: ConfigEntry,
    target_subentry: ConfigSubentry,
) -> None:
    entity_reg = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(entity_reg, source_entry.entry_id):
        entity_reg.async_update_entity(
            entity_entry.entity_id,
            config_entry_id=target_entry.entry_id,
            config_subentry_id=target_subentry.subentry_id,
        )


@callback
def _clear_entities_from_subentries(
    hass,
    config_entry: ConfigEntry,
) -> None:
    """Keep chip entities attached to the parent entry."""
    entity_reg = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(entity_reg, config_entry.entry_id):
        if entity_entry.config_subentry_id is None:
            continue
        entity_reg.async_update_entity(
            entity_entry.entity_id,
            config_subentry_id=None,
        )


async def async_migrate_integration(hass) -> None:
    """Migrate legacy per-pin entries to chip entries with pin subentries."""
    entries = sorted(
        hass.config_entries.async_entries(DOMAIN),
        key=lambda entry: entry.disabled_by is not None,
    )
    legacy_entries = [entry for entry in entries if entry.version < 4]
    if not legacy_entries:
        return

    existing_chip_entries: dict[tuple[int, int], ConfigEntry] = {}
    for entry in entries:
        if CONF_I2C_BUS not in entry.data or CONF_I2C_ADDRESS not in entry.data:
            continue
        i2c_bus = int(entry.data[CONF_I2C_BUS])
        i2c_address = int(entry.data[CONF_I2C_ADDRESS])
        if entry.version >= 4:
            existing_chip_entries[(i2c_bus, i2c_address)] = entry

    grouped_entries: dict[tuple[int, int], list[ConfigEntry]] = defaultdict(list)
    for entry in legacy_entries:
        i2c_bus = int(entry.data.get(CONF_I2C_BUS, DEFAULT_I2C_BUS))
        i2c_address = int(entry.data[CONF_I2C_ADDRESS])
        grouped_entries[(i2c_bus, i2c_address)].append(entry)

    for (i2c_bus, i2c_address), legacy_chip_entries in grouped_entries.items():
        legacy_chip_entries.sort(key=lambda entry: entry.disabled_by is not None)
        parent_entry = legacy_chip_entries[0]
        if (i2c_bus, i2c_address) in existing_chip_entries:
            parent_entry = existing_chip_entries[(i2c_bus, i2c_address)]

        for legacy_entry in legacy_chip_entries:
            subentry_data = _legacy_subentry_data(legacy_entry)
            subentry = _ensure_subentry(hass, parent_entry, subentry_data)
            _migrate_entities_to_subentry(hass, legacy_entry, parent_entry, subentry)
            if legacy_entry.entry_id != parent_entry.entry_id:
                await hass.config_entries.async_remove(legacy_entry.entry_id)

        scan_rate = _normalize_scan_rate(
            parent_entry.data.get(CONF_SCAN_RATE, SCAN_RATE_DEFAULT)
        )
        hass.config_entries.async_update_entry(
            parent_entry,
            version=4,
            data={
                CONF_I2C_BUS: i2c_bus,
                CONF_I2C_ADDRESS: i2c_address,
                CONF_SCAN_RATE: scan_rate,
            },
            options={},
            unique_id=_chip_unique_id(i2c_bus, i2c_address),
            title=_chip_title(i2c_bus, i2c_address),
        )

    for entry in hass.config_entries.async_entries(DOMAIN):
        await async_migrate_entry_pin_configs(hass, entry)


async def async_migrate_entry(hass, config_entry):
    """Migrate old config entries."""
    _LOGGER.info("Migrating from version %s", config_entry.version)
    if config_entry.version > 5:
        return False
    if config_entry.version < 4:
        await async_migrate_integration(hass)
    await async_migrate_entry_pin_configs(hass, config_entry)
    if config_entry.version < 5:
        hass.config_entries.async_update_entry(config_entry, version=5)
    return True


async def async_setup(hass, config):
    """Set up the component."""

    global SCAN_RATE_DEFAULT, I2C_LOCKS_KEY

    hass.data.setdefault(DOMAIN, {})

    SCAN_RATE_DEFAULT = _normalize_scan_rate(
        config.get(DOMAIN, {}).get("scan_rate", DEFAULT_SCAN_RATE)
    )
    _LOGGER.info("MCP23017 default scan_rate set to %.3f second(s)", SCAN_RATE_DEFAULT)

    I2C_LOCKS_KEY = config.get(DOMAIN, {}).get("i2c_locks", DEFAULT_I2C_LOCKS_KEY)
    if I2C_LOCKS_KEY not in hass.data:
        hass.data[I2C_LOCKS_KEY] = {}

    await async_migrate_integration(hass)

    async def async_handle_run_pattern(call):
        data = call.data
        repeat_count = data.get(ATTR_PATTERN_REPEAT_COUNT)
        repeat_for_seconds = data.get(ATTR_PATTERN_REPEAT_FOR_SECONDS)
        if (repeat_count is None) == (repeat_for_seconds is None):
            raise HomeAssistantError(
                f"Specify exactly one of {ATTR_PATTERN_REPEAT_COUNT} or "
                f"{ATTR_PATTERN_REPEAT_FOR_SECONDS}"
            )

        requested_pins = [int(pin) for pin in data[ATTR_PATTERN_PINS]]
        pins = list(dict.fromkeys(requested_pins))
        if len(pins) != len(requested_pins):
            raise HomeAssistantError("Pattern pin list contains duplicates")

        steps = data[ATTR_PATTERN_STEPS]

        i2c_bus = int(data[CONF_I2C_BUS])
        i2c_address = int(data[CONF_I2C_ADDRESS])
        pattern_name = _validate_pattern_name(data[ATTR_PATTERN_NAME])
        domain_id = MCP23017.domain_id(i2c_bus, i2c_address)
        component = hass.data[DOMAIN].get(domain_id)
        if component is None:
            raise HomeAssistantError(f"Chip {domain_id} is not loaded")

        await component.async_start_pattern(
            pattern_name,
            pins,
            steps,
            repeat_count=repeat_count,
            repeat_for_seconds=repeat_for_seconds,
        )

    async def async_handle_stop_pattern(call):
        data = call.data
        i2c_bus = int(data[CONF_I2C_BUS])
        i2c_address = int(data[CONF_I2C_ADDRESS])
        pattern_name = _validate_pattern_name(data[ATTR_PATTERN_NAME])
        domain_id = MCP23017.domain_id(i2c_bus, i2c_address)
        component = hass.data[DOMAIN].get(domain_id)
        if component is None:
            raise HomeAssistantError(f"Chip {domain_id} is not loaded")

        await component.async_stop_pattern(pattern_name)

    if not hass.services.has_service(DOMAIN, SERVICE_RUN_PATTERN):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_PATTERN,
            async_handle_run_pattern,
            schema=RUN_PATTERN_SERVICE_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_STOP_PATTERN):
        hass.services.async_register(
            DOMAIN,
            SERVICE_STOP_PATTERN,
            async_handle_stop_pattern,
            schema=STOP_PATTERN_SERVICE_SCHEMA,
        )

    def start_polling(event):
        for component in hass.data[DOMAIN].values():
            component.start_polling()

    async def stop_polling(event):
        for component in hass.data[DOMAIN].values():
            await component.async_stop_all_patterns()
            await component.stop_polling()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, start_polling)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_polling)
    return True


async def _async_entry_updated(hass, config_entry):
    """Reload entry when parent entry or subentries are updated."""
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_setup_entry(hass, config_entry):
    """Set up the MCP23017 from a config entry."""
    with setup_entry_status:
        await async_migrate_entry_pin_configs(hass, config_entry)

        _clear_entities_from_subentries(hass, config_entry)
        await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    config_entry.async_on_unload(config_entry.add_update_listener(_async_entry_updated))
    return True


async def async_unload_entry(hass, config_entry):
    """Unload chip entry and platforms."""
    if not await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS):
        return False

    i2c_address = int(config_entry.data[CONF_I2C_ADDRESS])
    i2c_bus = int(config_entry.data[CONF_I2C_BUS])
    domain_id = MCP23017.domain_id(i2c_bus, i2c_address)

    async with MCP23017_DATA_LOCK:
        component = hass.data[DOMAIN].get(domain_id)
        if component and component.has_no_entities:
            await component.async_stop_all_patterns()
            await component.stop_polling()
            hass.data[DOMAIN].pop(domain_id)
            _LOGGER.info("%s component destroyed", component.unique_id)

    return True


async def async_get_or_create(hass, config_entry, entity, register_entity=True):
    """Get or create a MCP23017 component from entity i2c address."""
    i2c_address = entity.address
    i2c_bus = entity.bus
    scan_rate = _normalize_scan_rate(
        config_entry.data.get(CONF_SCAN_RATE, SCAN_RATE_DEFAULT)
    )
    readout_enabled = bool(
        config_entry.data.get(CONF_READOUT_ENABLED, DEFAULT_READOUT_ENABLED)
    )
    domain_id = MCP23017.domain_id(i2c_bus, i2c_address)

    try:
        async with MCP23017_DATA_LOCK:
            if domain_id in hass.data[DOMAIN]:
                component = hass.data[DOMAIN][domain_id]
            else:
                component = await hass.async_add_executor_job(
                    functools.partial(MCP23017, hass, i2c_bus, i2c_address, scan_rate)
                )
                component.readout_enabled = readout_enabled
                hass.data[DOMAIN][domain_id] = component

                if hass.is_running:
                    component.start_polling()

                devices = device_registry.async_get(hass)
                devices.async_get_or_create(
                    config_entry_id=config_entry.entry_id,
                    identifiers={(DOMAIN, i2c_bus, i2c_address)},
                    manufacturer="MicroChip",
                    model=DOMAIN,
                    name=component.unique_id,
                )

            if register_entity:
                await hass.async_add_executor_job(
                    functools.partial(component.register_entity, entity)
                )
    except ValueError as error:
        component = None
        persistent_notification.create(
            hass,
            f"Error: Unable to access {DOMAIN}:{domain_id} ({error})",
            title=f"{DOMAIN} Configuration",
            notification_id=f"{DOMAIN} notification",
        )

    return component


def i2c_device_exist(bus, address):
    try:
        with smbus2.SMBus(bus) as i2c_bus:
            i2c_bus.read_byte(address)
    except (FileNotFoundError, OSError):
        return False
    return True


class MCP23017:
    """MCP23017 device driver."""

    def __init__(self, hass, bus, address, scan_rate=DEFAULT_SCAN_RATE):
        """Create a MCP23017 instance at {address} on I2C {bus}."""
        self._address = address
        self._busNumber = bus
        self._scan_rate = _normalize_scan_rate(scan_rate)
        self._readout_enabled = DEFAULT_READOUT_ENABLED
        self.hass = hass
        self._i2c_fault_count = 0

        self._device_lock, created = get_i2c_bus_lock(hass, I2C_LOCKS_KEY, bus)
        if created:
            _LOGGER.warning("MCP23017 Created new lock for I2C bus %s", bus)

        # Check device presence
        try:
            self._bus = smbus2.SMBus(self._busNumber)
            self._bus.read_byte(self._address)
        except (FileNotFoundError, OSError) as error:
            _LOGGER.error(
                "Unable to access %s (%s)",
                self.unique_id,
                error,
            )
            raise ValueError(error) from error

        try:
            # Change register map (IOCON.BANK = 1) to support/make it compatible with MCP23008
            # - Note: when BANK is already set to 1, e.g. HA restart without power cycle,
            #   IOCON_REMAP address is not mapped and write is ignored
            self[IOCON_REMAP] = self[IOCON_REMAP] | 0x80

            self._cache = {
                "IODIR": (self[REGISTER_MAP["IODIRB"]] << 8) + self[REGISTER_MAP["IODIRA"]],
                "GPPU": (self[REGISTER_MAP["GPPUB"]] << 8) + self[REGISTER_MAP["GPPUA"]],
                "GPIO": (self[REGISTER_MAP["GPIOB"]] << 8) + self[REGISTER_MAP["GPIOA"]],
                "OLAT": (self[REGISTER_MAP["OLATB"]] << 8) + self[REGISTER_MAP["OLATA"]],
            }
        except TypeError as error:
            raise ValueError(
                f"I2C read failure during {self.unique_id} initialization"
            ) from error

        self._run = False
        self._task = None
        self._entities = [None for i in range(16)]
        self._update_bitmap = 0
        self._pattern_tasks = {}
        self._pattern_pins = {}
        self._pattern_lock = asyncio.Lock()

        _LOGGER.info("%s device created", self.unique_id)
    
    async def __aenter__(self):
        await self._device_lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._device_lock.release()
        return False

    async def _async_i2c_call(self, func, *args):
        async with self:
            return await self.hass.async_add_executor_job(func, *args)

    async def async_get_pin_value(self, pin):
        return await self._async_i2c_call(self.get_pin_value, pin)

    async def async_set_pin_value(self, pin, value):
        await self._async_i2c_call(self.set_pin_value, pin, value)

    async def async_set_input(self, pin, is_input):
        await self._async_i2c_call(self.set_input, pin, is_input)

    async def async_set_pullup(self, pin, is_pullup):
        await self._async_i2c_call(self.set_pullup, pin, is_pullup)

    def __setitem__(self, register, value):
        """Set MCP23017 {register} to {value}."""
        try:
            self._bus.write_byte_data(self._address, register, value)
            if self._i2c_fault_count > 0:
                _LOGGER.info(
                    "I2C access recovered for %s after %d error(s)",
                    self.unique_id,
                    self._i2c_fault_count,
                )
                self._i2c_fault_count = 0
        except (OSError) as error:
            self._i2c_fault_count += 1
            if self._i2c_fault_count == 1:
                _LOGGER.error(
                    "I2C write failure %s [0x%02x] <- 0x%02x (%s); suppressing until recovery",
                    self.unique_id,
                    register,
                    value,
                    error,
                )

    def __getitem__(self, register):
        """Get value of MCP23017 {register}."""
        try:
            data = self._bus.read_byte_data(self._address, register)
            if self._i2c_fault_count > 0:
                _LOGGER.info(
                    "I2C access recovered for %s after %d error(s)",
                    self.unique_id,
                    self._i2c_fault_count,
                )
                self._i2c_fault_count = 0
        except (OSError) as error:
            data = None
            self._i2c_fault_count += 1
            if self._i2c_fault_count == 1:
                _LOGGER.error(
                    "I2C read failure %s [0x%02x] (%s); suppressing until recovery",
                    self.unique_id,
                    register,
                    error,
                )
        return data

    def _get_register_value(self, register, bit):
        """Get MCP23017 {bit} of {register}."""
        if bit < 8:
            reg = REGISTER_MAP[f"{register}A"]
            value = self[reg]
            if value is not None:
                self._cache[register] = (self._cache[register] & 0xFF00) | (value & 0xFF)
        else:
            reg = REGISTER_MAP[f"{register}B"]
            value = self[reg]
            if value is not None:
                self._cache[register] = (self._cache[register] & 0x00FF) | ((value & 0xFF) << 8)
        return bool(self._cache[register] & (1 << bit))


    def _set_register_value(self, register, bit, value):
        """Set MCP23017 {bit} of {register} to {value}."""
        cache_old = self._cache[register]
        if value:
            self._cache[register] |= (1 << bit) & 0xFFFF
        else:
            self._cache[register] &= ~(1 << bit) & 0xFFFF

        if cache_old != self._cache[register]:
            if bit < 8:
                reg = REGISTER_MAP[f"{register}A"]
                self[reg] = self._cache[register] & 0xFF
            else:
                reg = REGISTER_MAP[f"{register}B"]
                self[reg] = (self._cache[register] >> 8) & 0xFF


    @property
    def address(self):
        """Return device address."""
        return self._address

    @property
    def bus(self):
        """Return device bus."""
        return self._busNumber

    @property
    def scan_rate(self):
        """Return polling scan rate."""
        return self._scan_rate

    @scan_rate.setter
    def scan_rate(self, value):
        """Set polling scan rate."""
        self._scan_rate = _normalize_scan_rate(value)

    @property
    def readout_enabled(self):
        """Return whether periodic readout is enabled."""
        return self._readout_enabled

    @readout_enabled.setter
    def readout_enabled(self, value):
        """Enable or disable periodic readout."""
        self._readout_enabled = bool(value)

    @staticmethod
    def domain_id(i2c_bus, i2c_address):
        """Returns address decorated with bus"""
        return f"{i2c_bus}:0x{i2c_address:02x}"

    @property
    def unique_id(self):
        """Return component unique id."""
        return f"{DOMAIN}:{self.domain_id(self.bus, self.address)}"

    @property
    def has_no_entities(self):
        """Check if there are no more entities attached."""
        return not any(self._entities)

    # -- Called from HA thread pool

    def get_pin_value(self, pin):
        """Get MCP23017 GPIO[{pin}] value."""
        _LOGGER.debug("Get %s GPIO[%d] value", self.unique_id, pin)
        return self._get_register_value("GPIO", pin)

    def set_pin_value(self, pin, value):
        """Set MCP23017 GPIO[{pin}] to {value}."""
        self._set_register_value("OLAT", pin, value)

    def set_input(self, pin, is_input):
        """Set MCP23017 GPIO[{pin}] as input."""
        self._set_register_value("IODIR", pin, is_input)

    def set_pullup(self, pin, is_pullup):
        """Set MCP23017 GPIO[{pin}] as pullup."""
        self._set_register_value("GPPU", pin, is_pullup)

    def register_entity(self, entity):
        """Register entity to this device instance."""
        self._entities[entity.pin] = entity

        # Trigger a callback to update initial state
        self._update_bitmap |= (1 << entity.pin) & 0xFFFF

        _LOGGER.info(
            "%s(pin %d:'%s') attached to %s",
            type(entity).__name__,
            entity.pin,
            entity.name,
            self.unique_id,
        )

        return True

    def unregister_entity(self, pin_number):
        """Unregister entity from the device."""

        entity = self._entities[pin_number]
        if entity is None:
            return
        if hasattr(entity, "unsubscribe_update_listener"):
            entity.unsubscribe_update_listener()
        self._entities[pin_number] = None

        _LOGGER.info(
            "%s(pin %d:'%s') removed from %s",
            type(entity).__name__,
            entity.pin,
            entity.name,
            self.unique_id,
        )

    def _is_pattern_output_pin(self, pin):
        entity = self._entities[pin]
        return (
            entity is not None
            and hasattr(entity, "async_turn_on")
            and hasattr(entity, "async_turn_off")
        )

    async def _async_apply_pattern_pin_state(self, pin, state):
        await self.async_set_pin_value(pin, state)
        entity = self._entities[pin]
        if entity is None:
            return
        invert_logic = bool(getattr(entity, "_invert_logic", False))
        if hasattr(entity, "_state"):
            entity._state = bool(state ^ invert_logic)
        if hasattr(entity, "schedule_update_ha_state"):
            entity.schedule_update_ha_state()

    async def async_start_pattern(
        self,
        pattern_name,
        pins,
        steps,
        *,
        repeat_count=None,
        repeat_for_seconds=None,
    ):
        """Start or replace a named pattern on this chip."""
        if not pins:
            raise HomeAssistantError("Pattern pins list must not be empty")
        if not steps:
            raise HomeAssistantError("Pattern must define at least one step")
        if (repeat_count is None) == (repeat_for_seconds is None):
            raise HomeAssistantError(
                f"Specify exactly one of {ATTR_PATTERN_REPEAT_COUNT} or "
                f"{ATTR_PATTERN_REPEAT_FOR_SECONDS}"
            )

        for pin in pins:
            if not self._is_pattern_output_pin(pin):
                raise HomeAssistantError(
                    f"Pin {pin} is not configured as an output switch on {self.unique_id}"
                )

        await self.async_stop_pattern(pattern_name, missing_ok=True)

        async with self._pattern_lock:
            requested_pins = set(pins)
            for active_name, active_pins in self._pattern_pins.items():
                if active_name == pattern_name:
                    continue
                if requested_pins.intersection(active_pins):
                    raise HomeAssistantError(
                        f"Pattern '{pattern_name}' conflicts with running pattern "
                        f"'{active_name}' on pins {sorted(requested_pins.intersection(active_pins))}"
                    )

            task = self.hass.loop.create_task(
                self._async_pattern_loop(
                    pattern_name,
                    tuple(pins),
                    tuple(steps),
                    repeat_count=repeat_count,
                    repeat_for_seconds=repeat_for_seconds,
                )
            )
            self._pattern_tasks[pattern_name] = task
            self._pattern_pins[pattern_name] = set(pins)

    async def async_stop_pattern(self, pattern_name, *, missing_ok=False):
        """Stop a named pattern."""
        async with self._pattern_lock:
            task = self._pattern_tasks.pop(pattern_name, None)
            self._pattern_pins.pop(pattern_name, None)

        if task is None:
            if missing_ok:
                return
            raise HomeAssistantError(f"Pattern '{pattern_name}' is not running")

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def async_stop_all_patterns(self):
        """Stop all running patterns on this chip."""
        async with self._pattern_lock:
            names = list(self._pattern_tasks)
        for pattern_name in names:
            await self.async_stop_pattern(pattern_name, missing_ok=True)

    async def _async_pattern_loop(
        self,
        pattern_name,
        pins,
        steps,
        *,
        repeat_count=None,
        repeat_for_seconds=None,
    ):
        """Execute one named pattern until finished or cancelled."""
        loops = 0
        start = monotonic()
        deadline = (
            start + float(repeat_for_seconds)
            if repeat_for_seconds is not None
            else None
        )
        task = asyncio.current_task()

        try:
            while True:
                for state, duration_ms in steps:
                    if deadline is not None and monotonic() >= deadline:
                        return
                    for pin in pins:
                        await self._async_apply_pattern_pin_state(pin, state)
                    sleep_time = duration_ms / 1000.0
                    if deadline is not None:
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            return
                        sleep_time = min(sleep_time, remaining)
                    await asyncio.sleep(sleep_time)

                loops += 1
                if repeat_count is not None and loops >= int(repeat_count):
                    break
                if (
                    deadline is not None
                    and monotonic() >= deadline
                ):
                    break
        finally:
            for pin in pins:
                await self._async_apply_pattern_pin_state(pin, False)
            async with self._pattern_lock:
                if self._pattern_tasks.get(pattern_name) is task:
                    self._pattern_tasks.pop(pattern_name, None)
                    self._pattern_pins.pop(pattern_name, None)

    def _poll_once_sync(self):
        # Read pin values for bank A and B from device only if there are associated callbacks (minimize # of I2C transactions)
        input_state = self._cache["GPIO"]
        if any(
            hasattr(entity, "push_update") for entity in self._entities[0:8]
        ):
            value = self[REGISTER_MAP["GPIOA"]]
            if value is not None:
                input_state = (input_state & 0xFF00) | value
        if any(
            hasattr(entity, "push_update") for entity in self._entities[8:16]
        ):
            value = self[REGISTER_MAP["GPIOB"]]
            if value is not None:
                input_state = (input_state & 0x00FF) | (value << 8)

        # Check pin values that changed and update input cache
        self._update_bitmap |= (input_state ^ self._cache["GPIO"])
        self._cache["GPIO"] = input_state
        # Call callback functions only for pin that changed
        for pin in range(16):
            entity = self._entities[pin]
            if (self._update_bitmap & 0x1) and entity and hasattr(entity, "push_update"):
                entity.push_update(bool(input_state & 0x1))
            input_state >>= 1
            self._update_bitmap >>= 1
    
    def start_polling(self):
        """Start async polling task."""
        if self._run:
            return
        self._run = True
        self._task = self.hass.loop.create_task(self._poll_loop())

    async def stop_polling(self):
        """Stop async polling task."""
        if not self._run:
            return
        self._run = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll_loop(self):
        """Poll all ports once and call corresponding callback if a change is detected."""

        _LOGGER.info("%s start polling task", self.unique_id)

        try:
            while self._run:
                if self._readout_enabled:
                    await self._async_i2c_call(self._poll_once_sync)
                await asyncio.sleep(self._scan_rate)
        except asyncio.CancelledError:
            _LOGGER.info("%s polling task cancelled", self.unique_id)
        finally:
            _LOGGER.info("%s stop polling task", self.unique_id)
