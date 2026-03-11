"""Support for I2C MCP23017 chip."""

import asyncio
import functools
import logging
from pathlib import Path

import smbus2

from homeassistant.const import EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry

from .const import (
    CONF_HW_SYNC,
    CONF_I2C_ADDRESS,
    CONF_I2C_BUS,
    CONF_INVERT_LOGIC,
    CONF_MOMENTARY,
    CONF_PIN_CONFIGS,
    CONF_PIN_MODE,
    CONF_POLL_BANK_A,
    CONF_POLL_BANK_B,
    CONF_PULSE_TIME,
    CONF_PULL_MODE,
    CONF_SCAN_RATE,
    DEFAULT_HW_SYNC,
    DEFAULT_I2C_BUS,
    DEFAULT_I2C_LOCKS_KEY,
    DEFAULT_INVERT_LOGIC,
    DEFAULT_MOMENTARY,
    DEFAULT_POLL_BANK_A,
    DEFAULT_POLL_BANK_B,
    DEFAULT_PULSE_TIME,
    DEFAULT_PULL_MODE,
    DEFAULT_SCAN_RATE,
    DOMAIN,
    MODE_DOWN,
    MODE_UP,
    PIN_MODE_DISABLED,
    PIN_MODE_INPUT,
    PIN_MODE_OUTPUT,
    TOTAL_PIN_COUNT,
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
    "OLATA": 0x0A,
    "OLATB": 0x1A,
}

# Register address used to toggle IOCON.BANK to 1 (only mapped when BANK is 0)
IOCON_REMAP = 0x0B

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["binary_sensor", "switch"]

I2C_LOCKS_KEY = DEFAULT_I2C_LOCKS_KEY
ENTRY_DOMAIN_MAP = f"{DOMAIN}_entry_domain"
UPDATE_LISTENER_MAP = f"{DOMAIN}_update_listener"

LEGACY_CONF_FLOW_PLATFORM = "platform"
LEGACY_CONF_FLOW_PIN_NUMBER = "pin_number"


def default_pin_config() -> dict:
    """Return default config for one pin."""
    return {
        CONF_PIN_MODE: PIN_MODE_INPUT,
        CONF_INVERT_LOGIC: DEFAULT_INVERT_LOGIC,
        CONF_PULL_MODE: DEFAULT_PULL_MODE,
        CONF_HW_SYNC: DEFAULT_HW_SYNC,
        CONF_MOMENTARY: DEFAULT_MOMENTARY,
        CONF_PULSE_TIME: DEFAULT_PULSE_TIME,
    }


def normalize_pin_config(raw: dict | None) -> dict:
    """Normalize one pin config dictionary."""
    config = default_pin_config()
    if isinstance(raw, dict):
        config[CONF_PIN_MODE] = raw.get(CONF_PIN_MODE, config[CONF_PIN_MODE])
        if config[CONF_PIN_MODE] not in {
            PIN_MODE_INPUT,
            PIN_MODE_OUTPUT,
            PIN_MODE_DISABLED,
        }:
            config[CONF_PIN_MODE] = PIN_MODE_INPUT
        config[CONF_INVERT_LOGIC] = bool(
            raw.get(CONF_INVERT_LOGIC, config[CONF_INVERT_LOGIC])
        )
        config[CONF_PULL_MODE] = raw.get(CONF_PULL_MODE, config[CONF_PULL_MODE])
        if config[CONF_PULL_MODE] not in {MODE_UP, MODE_DOWN}:
            config[CONF_PULL_MODE] = MODE_UP
        config[CONF_HW_SYNC] = bool(raw.get(CONF_HW_SYNC, config[CONF_HW_SYNC]))
        config[CONF_MOMENTARY] = bool(raw.get(CONF_MOMENTARY, config[CONF_MOMENTARY]))
        try:
            pulse_time = int(raw.get(CONF_PULSE_TIME, config[CONF_PULSE_TIME]))
        except (TypeError, ValueError):
            pulse_time = DEFAULT_PULSE_TIME
        config[CONF_PULSE_TIME] = max(0, pulse_time)
    return config


def normalize_pin_configs(raw_pin_configs: list | None) -> list[dict]:
    """Normalize list of per-pin configuration dictionaries."""
    pin_configs = []
    for pin in range(TOTAL_PIN_COUNT):
        raw = None
        if isinstance(raw_pin_configs, list) and pin < len(raw_pin_configs):
            raw = raw_pin_configs[pin]
        pin_configs.append(normalize_pin_config(raw))
    return pin_configs


def _legacy_pin_configs(data: dict, options: dict) -> list[dict]:
    """Convert legacy per-pin entry data into the new pin config list."""
    pin_configs = []
    for _ in range(TOTAL_PIN_COUNT):
        disabled_pin = default_pin_config()
        disabled_pin[CONF_PIN_MODE] = PIN_MODE_DISABLED
        pin_configs.append(disabled_pin)

    try:
        pin_number = int(data.get(LEGACY_CONF_FLOW_PIN_NUMBER, 0))
    except (TypeError, ValueError):
        pin_number = 0
    if pin_number < 0 or pin_number >= TOTAL_PIN_COUNT:
        pin_number = 0

    mode = (
        PIN_MODE_OUTPUT
        if data.get(LEGACY_CONF_FLOW_PLATFORM) == "switch"
        else PIN_MODE_INPUT
    )
    pin_config = default_pin_config()
    pin_config[CONF_PIN_MODE] = mode
    pin_config[CONF_INVERT_LOGIC] = bool(
        options.get(CONF_INVERT_LOGIC, data.get(CONF_INVERT_LOGIC, DEFAULT_INVERT_LOGIC))
    )
    pin_config[CONF_PULL_MODE] = options.get(
        CONF_PULL_MODE,
        data.get(CONF_PULL_MODE, DEFAULT_PULL_MODE),
    )
    if pin_config[CONF_PULL_MODE] not in {MODE_UP, MODE_DOWN}:
        pin_config[CONF_PULL_MODE] = DEFAULT_PULL_MODE
    pin_config[CONF_HW_SYNC] = bool(
        options.get(CONF_HW_SYNC, data.get(CONF_HW_SYNC, DEFAULT_HW_SYNC))
    )
    pin_config[CONF_MOMENTARY] = bool(
        options.get(CONF_MOMENTARY, data.get(CONF_MOMENTARY, DEFAULT_MOMENTARY))
    )
    try:
        pin_config[CONF_PULSE_TIME] = max(
            0,
            int(options.get(CONF_PULSE_TIME, data.get(CONF_PULSE_TIME, DEFAULT_PULSE_TIME))),
        )
    except (TypeError, ValueError):
        pin_config[CONF_PULSE_TIME] = DEFAULT_PULSE_TIME

    pin_configs[pin_number] = pin_config
    return pin_configs


def get_entry_config(config_entry) -> dict:
    """Return effective chip configuration from entry data + options."""
    data = dict(config_entry.data)
    options = dict(config_entry.options)

    try:
        scan_rate = float(options.get(CONF_SCAN_RATE, data.get(CONF_SCAN_RATE, DEFAULT_SCAN_RATE)))
    except (TypeError, ValueError):
        scan_rate = DEFAULT_SCAN_RATE
    scan_rate = max(0.01, scan_rate)

    raw_pin_configs = options.get(CONF_PIN_CONFIGS, data.get(CONF_PIN_CONFIGS))
    if raw_pin_configs is None and LEGACY_CONF_FLOW_PIN_NUMBER in data:
        pin_configs = _legacy_pin_configs(data, options)
    else:
        pin_configs = normalize_pin_configs(raw_pin_configs)

    return {
        CONF_I2C_BUS: int(data[CONF_I2C_BUS]),
        CONF_I2C_ADDRESS: int(data[CONF_I2C_ADDRESS]),
        CONF_SCAN_RATE: scan_rate,
        CONF_POLL_BANK_A: bool(
            options.get(CONF_POLL_BANK_A, data.get(CONF_POLL_BANK_A, DEFAULT_POLL_BANK_A))
        ),
        CONF_POLL_BANK_B: bool(
            options.get(CONF_POLL_BANK_B, data.get(CONF_POLL_BANK_B, DEFAULT_POLL_BANK_B))
        ),
        CONF_PIN_CONFIGS: pin_configs,
    }


def list_i2c_buses() -> list[int]:
    """Discover available /dev/i2c-* buses."""
    buses: list[int] = []
    for path in Path("/dev").glob("i2c-*"):
        suffix = path.name.split("-", maxsplit=1)[-1]
        if suffix.isdigit():
            buses.append(int(suffix))
    return sorted(set(buses)) or [DEFAULT_I2C_BUS]


def i2c_device_exist(bus: int, address: int) -> bool:
    """Check if an I2C address responds on a bus."""
    try:
        with smbus2.SMBus(bus) as i2c_bus:
            i2c_bus.read_byte(address)
    except (FileNotFoundError, OSError):
        return False
    return True


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the component."""
    global I2C_LOCKS_KEY

    hass.data.setdefault(DOMAIN, {})
    hass.data.setdefault(ENTRY_DOMAIN_MAP, {})
    hass.data.setdefault(UPDATE_LISTENER_MAP, {})

    I2C_LOCKS_KEY = config.get(DOMAIN, {}).get("i2c_locks", DEFAULT_I2C_LOCKS_KEY)
    hass.data.setdefault(I2C_LOCKS_KEY, {})

    async def _stop_all_components(_event):
        for component in list(hass.data[DOMAIN].values()):
            await component.stop_polling()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_all_components)
    return True


async def _async_reload_entry(hass: HomeAssistant, config_entry) -> None:
    """Reload entry after options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, config_entry) -> bool:
    """Set up one MCP23017 chip from a config entry."""
    entry_config = get_entry_config(config_entry)
    i2c_bus = entry_config[CONF_I2C_BUS]
    i2c_address = entry_config[CONF_I2C_ADDRESS]
    domain_id = MCP23017.domain_id(i2c_bus, i2c_address)
    component = hass.data[DOMAIN].get(domain_id)
    component_created = component is None

    if component_created:
        try:
            component = await hass.async_add_executor_job(
                functools.partial(
                    MCP23017,
                    hass,
                    i2c_bus,
                    i2c_address,
                    entry_config[CONF_SCAN_RATE],
                    entry_config[CONF_POLL_BANK_A],
                    entry_config[CONF_POLL_BANK_B],
                    entry_config[CONF_PIN_CONFIGS],
                )
            )
        except ValueError as error:
            raise ConfigEntryNotReady(
                f"Unable to access {DOMAIN}:{i2c_bus}:0x{i2c_address:02x} ({error})"
            ) from error
        hass.data[DOMAIN][domain_id] = component

    hass.data[ENTRY_DOMAIN_MAP][config_entry.entry_id] = domain_id
    hass.data[UPDATE_LISTENER_MAP][
        config_entry.entry_id
    ] = config_entry.add_update_listener(_async_reload_entry)

    devices = device_registry.async_get(hass)
    devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, i2c_bus, i2c_address)},
        manufacturer="Microchip",
        model="MCP23017",
        name=component.unique_id,
    )

    try:
        await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    except Exception:
        update_listener = hass.data[UPDATE_LISTENER_MAP].pop(config_entry.entry_id, None)
        if update_listener:
            update_listener()
        hass.data[ENTRY_DOMAIN_MAP].pop(config_entry.entry_id, None)
        if component_created:
            await component.stop_polling()
            await component.close()
            hass.data[DOMAIN].pop(domain_id, None)
        raise

    if component_created:
        if hass.is_running:
            component.start_polling()
        else:
            @callback
            def _start_component(_event):
                active_component = hass.data[DOMAIN].get(domain_id)
                if active_component:
                    active_component.start_polling()

            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _start_component)

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry) -> bool:
    """Unload one chip entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
    if not unload_ok:
        return False

    update_listener = hass.data[UPDATE_LISTENER_MAP].pop(
        config_entry.entry_id,
        None,
    )
    if update_listener:
        update_listener()

    domain_id = hass.data[ENTRY_DOMAIN_MAP].pop(config_entry.entry_id, None)
    component = hass.data[DOMAIN].get(domain_id) if domain_id else None
    if component and component.has_no_entities:
        await component.stop_polling()
        await component.close()
        hass.data[DOMAIN].pop(domain_id, None)

    return True


def async_get_component(hass: HomeAssistant, config_entry):
    """Return the component bound to one config entry."""
    domain_id = hass.data[ENTRY_DOMAIN_MAP].get(config_entry.entry_id)
    if domain_id is None:
        return None
    return hass.data[DOMAIN].get(domain_id)


class MCP23017:
    """MCP23017 device driver."""

    def __init__(
        self,
        hass: HomeAssistant,
        bus: int,
        address: int,
        scan_rate: float,
        poll_bank_a: bool,
        poll_bank_b: bool,
        pin_configs: list[dict],
    ):
        """Create a MCP23017 instance at {address} on I2C {bus}."""
        self._address = address
        self._bus_number = bus
        self._scan_rate = max(0.01, scan_rate)
        self._poll_bank_a = poll_bank_a
        self._poll_bank_b = poll_bank_b
        self._pin_configs = pin_configs
        self.hass = hass
        self._closed = False

        self._device_lock, created = get_i2c_bus_lock(hass, I2C_LOCKS_KEY, bus)
        if created:
            _LOGGER.warning("MCP23017 created new lock for I2C bus %s", bus)

        try:
            self._bus = smbus2.SMBus(self._bus_number)
            self._bus.read_byte(self._address)
        except (FileNotFoundError, OSError) as error:
            _LOGGER.error("Unable to access %s (%s)", self.unique_id, error)
            raise ValueError(error) from error

        # Change register map (IOCON.BANK = 1) to support MCP23008-compatible mapping.
        self[IOCON_REMAP] = self[IOCON_REMAP] | 0x80

        self._run = False
        self._task = None
        self._cache = {
            "IODIR": (self[REGISTER_MAP["IODIRB"]] << 8) + self[REGISTER_MAP["IODIRA"]],
            "GPPU": (self[REGISTER_MAP["GPPUB"]] << 8) + self[REGISTER_MAP["GPPUA"]],
            "GPIO": (self[REGISTER_MAP["GPIOB"]] << 8) + self[REGISTER_MAP["GPIOA"]],
            "OLAT": (self[REGISTER_MAP["OLATB"]] << 8) + self[REGISTER_MAP["OLATA"]],
        }
        self._entities = [None for _ in range(TOTAL_PIN_COUNT)]
        self._update_bitmap = 0

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

    async def async_get_pin_value(self, pin: int) -> bool:
        """Get pin value."""
        return await self._async_i2c_call(self.get_pin_value, pin)

    async def async_set_pin_value(self, pin: int, value: bool) -> None:
        """Set pin output level."""
        await self._async_i2c_call(self.set_pin_value, pin, value)

    async def async_set_input(self, pin: int, is_input: bool) -> None:
        """Set pin direction."""
        await self._async_i2c_call(self.set_input, pin, is_input)

    async def async_set_pullup(self, pin: int, is_pullup: bool) -> None:
        """Set pin pull-up mode."""
        await self._async_i2c_call(self.set_pullup, pin, is_pullup)

    def __setitem__(self, register: int, value: int) -> None:
        """Set MCP23017 {register} to {value}."""
        self._bus.write_byte_data(self._address, register, value)

    def __getitem__(self, register: int) -> int:
        """Get value of MCP23017 {register}."""
        return self._bus.read_byte_data(self._address, register)

    def _get_register_value(self, register: str, bit: int) -> bool:
        """Get MCP23017 {bit} of {register}."""
        if bit < 8:
            reg = REGISTER_MAP[f"{register}A"]
            value = self[reg] & 0xFF
            self._cache[register] = (self._cache[register] & 0xFF00) | value
        else:
            reg = REGISTER_MAP[f"{register}B"]
            value = self[reg] & 0xFF
            self._cache[register] = (self._cache[register] & 0x00FF) | (value << 8)
        return bool(self._cache[register] & (1 << bit))

    def _set_register_value(self, register: str, bit: int, value: bool) -> None:
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
    def address(self) -> int:
        """Return device address."""
        return self._address

    @property
    def bus(self) -> int:
        """Return device bus."""
        return self._bus_number

    @staticmethod
    def domain_id(i2c_bus: int, i2c_address: int) -> str:
        """Return address decorated with bus."""
        return f"{i2c_bus}:0x{i2c_address:02x}"

    @property
    def unique_id(self) -> str:
        """Return component unique id."""
        return f"{DOMAIN}:{self.domain_id(self.bus, self.address)}"

    @property
    def has_no_entities(self) -> bool:
        """Return True when no entities are currently attached."""
        return not any(self._entities)

    # -- Called from HA thread pool

    def get_pin_value(self, pin: int) -> bool:
        """Get MCP23017 GPIO[{pin}] value."""
        return self._get_register_value("GPIO", pin)

    def set_pin_value(self, pin: int, value: bool) -> None:
        """Set MCP23017 GPIO[{pin}] to {value}."""
        self._set_register_value("OLAT", pin, value)

    def set_input(self, pin: int, is_input: bool) -> None:
        """Set MCP23017 GPIO[{pin}] as input."""
        self._set_register_value("IODIR", pin, is_input)

    def set_pullup(self, pin: int, is_pullup: bool) -> None:
        """Set MCP23017 GPIO[{pin}] pull-up mode."""
        self._set_register_value("GPPU", pin, is_pullup)

    def register_entity(self, entity) -> None:
        """Register entity to this device instance."""
        self._entities[entity.pin] = entity
        self._update_bitmap |= (1 << entity.pin) & 0xFFFF
        _LOGGER.info(
            "%s(pin %d) attached to %s",
            type(entity).__name__,
            entity.pin,
            self.unique_id,
        )

    def unregister_entity(self, pin_number: int) -> None:
        """Unregister entity from the device."""
        entity = self._entities[pin_number]
        self._entities[pin_number] = None
        if entity is not None:
            _LOGGER.info(
                "%s(pin %d) removed from %s",
                type(entity).__name__,
                entity.pin,
                self.unique_id,
            )

    def _poll_once_sync(self) -> None:
        """Read configured input banks and dispatch entity updates."""
        input_state = self._cache["GPIO"]

        bank_a_has_inputs = any(
            entity is not None and hasattr(entity, "push_update")
            for entity in self._entities[0:8]
        )
        bank_b_has_inputs = any(
            entity is not None and hasattr(entity, "push_update")
            for entity in self._entities[8:16]
        )

        if self._poll_bank_a and bank_a_has_inputs:
            input_state = (input_state & 0xFF00) | self[REGISTER_MAP["GPIOA"]]
        if self._poll_bank_b and bank_b_has_inputs:
            input_state = (input_state & 0x00FF) | (self[REGISTER_MAP["GPIOB"]] << 8)

        changed_bits = input_state ^ self._cache["GPIO"]
        self._update_bitmap |= changed_bits
        self._cache["GPIO"] = input_state

        for pin in range(TOTAL_PIN_COUNT):
            entity = self._entities[pin]
            if (
                entity is not None
                and (self._update_bitmap & (1 << pin))
                and hasattr(entity, "push_update")
            ):
                entity.push_update(bool(input_state & (1 << pin)))
        self._update_bitmap = 0

    def start_polling(self) -> None:
        """Start async polling task."""
        if self._run:
            return
        self._run = True
        self._task = self.hass.loop.create_task(self._poll_loop())

    async def stop_polling(self) -> None:
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

    async def close(self) -> None:
        """Close SMBus handle."""
        if self._closed:
            return
        self._closed = True
        await self.hass.async_add_executor_job(self._bus.close)

    async def _poll_loop(self) -> None:
        """Poll all input banks and push updates."""
        _LOGGER.info("%s start polling task", self.unique_id)
        try:
            while self._run:
                await self._async_i2c_call(self._poll_once_sync)
                await asyncio.sleep(self._scan_rate)
        except asyncio.CancelledError:
            _LOGGER.info("%s polling task cancelled", self.unique_id)
        finally:
            _LOGGER.info("%s stop polling task", self.unique_id)
