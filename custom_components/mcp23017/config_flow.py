"""Config flow for MCP23017 component."""

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from . import (
    get_entry_config,
    i2c_device_exist,
    list_i2c_buses,
    normalize_pin_configs,
)
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
    DEFAULT_I2C_ADDRESS,
    DEFAULT_I2C_BUS,
    DEFAULT_I2C_LOCKS_KEY,
    DEFAULT_POLL_BANK_A,
    DEFAULT_POLL_BANK_B,
    DEFAULT_SCAN_RATE,
    DOMAIN,
    MODE_DOWN,
    MODE_UP,
    PIN_MODE_INPUT,
    PIN_MODE_OUTPUT,
)
from .i2c_lock import get_i2c_bus_lock


def _pin_field(pin: int, key: str) -> str:
    return f"pin_{pin}_{key}"


def _chip_title(data: dict) -> str:
    return f"Bus: {data[CONF_I2C_BUS]}, address: 0x{data[CONF_I2C_ADDRESS]:02X}"


def _chip_unique_id(data: dict) -> str:
    return f"{DOMAIN}.{data[CONF_I2C_BUS]}.{data[CONF_I2C_ADDRESS]}"


def _build_general_schema(defaults: dict, include_chip_identity: bool) -> vol.Schema:
    schema: dict = {
        vol.Required(
            CONF_SCAN_RATE,
            default=defaults.get(CONF_SCAN_RATE, DEFAULT_SCAN_RATE),
        ): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=60)),
        vol.Required(
            CONF_POLL_BANK_A,
            default=defaults.get(CONF_POLL_BANK_A, DEFAULT_POLL_BANK_A),
        ): bool,
        vol.Required(
            CONF_POLL_BANK_B,
            default=defaults.get(CONF_POLL_BANK_B, DEFAULT_POLL_BANK_B),
        ): bool,
    }

    if include_chip_identity:
        buses = list_i2c_buses()
        bus_default = defaults.get(CONF_I2C_BUS, DEFAULT_I2C_BUS)
        if bus_default not in buses:
            bus_default = buses[0]

        schema = {
            vol.Required(CONF_I2C_BUS, default=bus_default): vol.In(
                {bus: f"/dev/i2c-{bus}" for bus in buses}
            ),
            vol.Required(
                CONF_I2C_ADDRESS,
                default=defaults.get(CONF_I2C_ADDRESS, DEFAULT_I2C_ADDRESS),
            ): vol.In(
                {
                    address: f"0x{address:02X}"
                    for address in range(0x20, 0x28)
                }
            ),
            **schema,
        }

    return vol.Schema(schema)


def _build_pin_bank_schema(pin_configs: list[dict], start_pin: int, end_pin: int) -> vol.Schema:
    mode_options = {
        PIN_MODE_INPUT: "Input (binary sensor)",
        PIN_MODE_OUTPUT: "Output (switch)",
    }
    pull_options = {
        MODE_UP: "UP (pull-up)",
        MODE_DOWN: "NONE (floating)",
    }

    schema: dict = {}
    for pin in range(start_pin, end_pin + 1):
        pin_config = pin_configs[pin]
        schema[vol.Required(
            _pin_field(pin, CONF_PIN_MODE),
            default=pin_config[CONF_PIN_MODE],
        )] = vol.In(mode_options)
        schema[vol.Required(
            _pin_field(pin, CONF_INVERT_LOGIC),
            default=pin_config[CONF_INVERT_LOGIC],
        )] = bool
        schema[vol.Required(
            _pin_field(pin, CONF_PULL_MODE),
            default=pin_config[CONF_PULL_MODE],
        )] = vol.In(pull_options)
        schema[vol.Required(
            _pin_field(pin, CONF_HW_SYNC),
            default=pin_config[CONF_HW_SYNC],
        )] = bool
        schema[vol.Required(
            _pin_field(pin, CONF_MOMENTARY),
            default=pin_config[CONF_MOMENTARY],
        )] = bool
        schema[vol.Required(
            _pin_field(pin, CONF_PULSE_TIME),
            default=pin_config[CONF_PULSE_TIME],
        )] = vol.All(vol.Coerce(int), vol.Range(min=0))
    return vol.Schema(schema)


def _apply_pin_bank_input(
    pin_configs: list[dict],
    user_input: dict,
    start_pin: int,
    end_pin: int,
) -> None:
    for pin in range(start_pin, end_pin + 1):
        pin_configs[pin][CONF_PIN_MODE] = user_input[_pin_field(pin, CONF_PIN_MODE)]
        pin_configs[pin][CONF_INVERT_LOGIC] = bool(
            user_input[_pin_field(pin, CONF_INVERT_LOGIC)]
        )
        pin_configs[pin][CONF_PULL_MODE] = user_input[_pin_field(pin, CONF_PULL_MODE)]
        pin_configs[pin][CONF_HW_SYNC] = bool(
            user_input[_pin_field(pin, CONF_HW_SYNC)]
        )
        pin_configs[pin][CONF_MOMENTARY] = bool(
            user_input[_pin_field(pin, CONF_MOMENTARY)]
        )
        pin_configs[pin][CONF_PULSE_TIME] = int(
            user_input[_pin_field(pin, CONF_PULSE_TIME)]
        )


class Mcp23017ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """MCP23017 config flow."""

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    def __init__(self):
        self._chip_config: dict = {}
        self._pin_configs: list[dict] = normalize_pin_configs(None)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Add support for config flow options."""
        return Mcp23017OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        """Configure one MCP23017 chip."""
        errors = {}
        if user_input is not None:
            self._chip_config = {
                CONF_I2C_BUS: int(user_input[CONF_I2C_BUS]),
                CONF_I2C_ADDRESS: int(user_input[CONF_I2C_ADDRESS]),
                CONF_SCAN_RATE: float(user_input[CONF_SCAN_RATE]),
                CONF_POLL_BANK_A: bool(user_input[CONF_POLL_BANK_A]),
                CONF_POLL_BANK_B: bool(user_input[CONF_POLL_BANK_B]),
            }

            await self.async_set_unique_id(_chip_unique_id(self._chip_config))
            self._abort_if_unique_id_configured()

            bus = self._chip_config[CONF_I2C_BUS]
            address = self._chip_config[CONF_I2C_ADDRESS]
            lock, _ = get_i2c_bus_lock(self.hass, DEFAULT_I2C_LOCKS_KEY, bus)
            async with lock:
                exists = await self.hass.async_add_executor_job(
                    i2c_device_exist,
                    bus,
                    address,
                )

            if not exists:
                errors["base"] = "invalid_i2c_address"
            else:
                return await self.async_step_pins_a()

        return self.async_show_form(
            step_id="user",
            data_schema=_build_general_schema(self._chip_config, include_chip_identity=True),
            errors=errors,
        )

    async def async_step_pins_a(self, user_input=None):
        """Configure pins 0..7."""
        if user_input is not None:
            _apply_pin_bank_input(self._pin_configs, user_input, 0, 7)
            return await self.async_step_pins_b()

        return self.async_show_form(
            step_id="pins_a",
            data_schema=_build_pin_bank_schema(self._pin_configs, 0, 7),
        )

    async def async_step_pins_b(self, user_input=None):
        """Configure pins 8..15 and create entry."""
        if user_input is not None:
            _apply_pin_bank_input(self._pin_configs, user_input, 8, 15)
            data = {
                **self._chip_config,
                CONF_PIN_CONFIGS: self._pin_configs,
            }
            return self.async_create_entry(
                title=_chip_title(data),
                data=data,
            )

        return self.async_show_form(
            step_id="pins_b",
            data_schema=_build_pin_bank_schema(self._pin_configs, 8, 15),
        )


class Mcp23017OptionsFlowHandler(config_entries.OptionsFlow):
    """MCP23017 options flow."""

    def __init__(self, config_entry):
        self.config_entry = config_entry
        effective = get_entry_config(config_entry)
        self._chip_options = {
            CONF_SCAN_RATE: effective[CONF_SCAN_RATE],
            CONF_POLL_BANK_A: effective[CONF_POLL_BANK_A],
            CONF_POLL_BANK_B: effective[CONF_POLL_BANK_B],
        }
        self._pin_configs = normalize_pin_configs(effective[CONF_PIN_CONFIGS])

    async def async_step_init(self, user_input=None):
        """Configure per-chip options."""
        if user_input is not None:
            self._chip_options = {
                CONF_SCAN_RATE: float(user_input[CONF_SCAN_RATE]),
                CONF_POLL_BANK_A: bool(user_input[CONF_POLL_BANK_A]),
                CONF_POLL_BANK_B: bool(user_input[CONF_POLL_BANK_B]),
            }
            return await self.async_step_pins_a()

        return self.async_show_form(
            step_id="init",
            data_schema=_build_general_schema(self._chip_options, include_chip_identity=False),
        )

    async def async_step_pins_a(self, user_input=None):
        """Configure options for pins 0..7."""
        if user_input is not None:
            _apply_pin_bank_input(self._pin_configs, user_input, 0, 7)
            return await self.async_step_pins_b()

        return self.async_show_form(
            step_id="pins_a",
            data_schema=_build_pin_bank_schema(self._pin_configs, 0, 7),
        )

    async def async_step_pins_b(self, user_input=None):
        """Configure options for pins 8..15."""
        if user_input is not None:
            _apply_pin_bank_input(self._pin_configs, user_input, 8, 15)
            return self.async_create_entry(
                title="",
                data={
                    **self._chip_options,
                    CONF_PIN_CONFIGS: self._pin_configs,
                },
            )

        return self.async_show_form(
            step_id="pins_b",
            data_schema=_build_pin_bank_schema(self._pin_configs, 8, 15),
        )
