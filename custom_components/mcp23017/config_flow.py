"""Config flow for MCP23017 component."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import voluptuous as vol

from homeassistant import config_entries, data_entry_flow
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import i2c_device_exist
from .const import (
    CONF_FLOW_PIN_NAME,
    CONF_FLOW_PIN_NUMBER,
    CONF_FLOW_PLATFORM,
    CONF_IMPORT_SUBENTRIES,
    CONF_I2C_ADDRESS,
    CONF_I2C_BUS,
    CONF_INVERT_LOGIC,
    CONF_PINS,
    CONF_PULL_MODE,
    CONF_HW_SYNC,
    CONF_MOMENTARY,
    CONF_PULSE_TIME,
    CONF_SCAN_RATE,
    DEFAULT_I2C_ADDRESS,
    DEFAULT_I2C_BUS,
    DEFAULT_I2C_LOCKS_KEY,
    DEFAULT_INVERT_LOGIC,
    DEFAULT_PULL_MODE,
    DEFAULT_HW_SYNC,
    DEFAULT_MOMENTARY,
    DEFAULT_PULSE_TIME,
    DEFAULT_SCAN_RATE,
    DOMAIN,
    PULL_MODE_NONE,
    PULL_MODE_UP,
    SUBENTRY_TYPE_PIN,
)
from .i2c_lock import get_i2c_bus_lock

PIN_PLATFORMS = ("binary_sensor", "switch")
MIN_SCAN_RATE = 0.01


def _chip_unique_id(i2c_bus: int, i2c_address: int) -> str:
    return f"{DOMAIN}.{i2c_bus}.{i2c_address}"


def _chip_title(i2c_bus: int, i2c_address: int) -> str:
    return f"Bus: {i2c_bus}, address: 0x{i2c_address:02x}"


def _pin_unique_id(platform: str, pin_number: int) -> str:
    return f"{platform}:{pin_number}"


def _pin_title(data: dict[str, Any]) -> str:
    return (
        f"{data[CONF_FLOW_PIN_NAME]} "
        f"({data[CONF_FLOW_PLATFORM]}, pin {data[CONF_FLOW_PIN_NUMBER]})"
    )


def _normalize_scan_rate(scan_rate: Any) -> float:
    try:
        value = float(scan_rate)
    except (TypeError, ValueError):
        return DEFAULT_SCAN_RATE
    return max(MIN_SCAN_RATE, value)


def _normalize_pull_mode(pull_mode: Any) -> str:
    if isinstance(pull_mode, str):
        return PULL_MODE_NONE if pull_mode.lower() == PULL_MODE_NONE else PULL_MODE_UP
    return DEFAULT_PULL_MODE


def _default_pin_name(i2c_bus: int, i2c_address: int, pin_number: int) -> str:
    return f"pin {i2c_bus}:0x{i2c_address:02x}:{pin_number}"


def _build_subentry_data(
    *,
    platform: str,
    user_input: dict[str, Any],
    i2c_bus: int,
    i2c_address: int,
) -> dict[str, Any]:
    pin_number = int(user_input[CONF_FLOW_PIN_NUMBER])
    pin_name = user_input.get(CONF_FLOW_PIN_NAME, "").strip()
    if not pin_name:
        pin_name = _default_pin_name(i2c_bus, i2c_address, pin_number)

    data: dict[str, Any] = {
        CONF_FLOW_PLATFORM: platform,
        CONF_FLOW_PIN_NUMBER: pin_number,
        CONF_FLOW_PIN_NAME: pin_name,
        CONF_INVERT_LOGIC: bool(user_input.get(CONF_INVERT_LOGIC, DEFAULT_INVERT_LOGIC)),
    }
    if platform == "binary_sensor":
        data[CONF_PULL_MODE] = _normalize_pull_mode(user_input.get(CONF_PULL_MODE))
    else:
        data[CONF_HW_SYNC] = bool(user_input.get(CONF_HW_SYNC, DEFAULT_HW_SYNC))
        data[CONF_MOMENTARY] = bool(user_input.get(CONF_MOMENTARY, DEFAULT_MOMENTARY))
        data[CONF_PULSE_TIME] = max(
            0,
            int(user_input.get(CONF_PULSE_TIME, DEFAULT_PULSE_TIME)),
        )
    return data


class Mcp23017PinSubentryFlowHandler(ConfigSubentryFlow):
    """Handle MCP23017 pin subentry flow."""

    @callback
    def _used_pins(self) -> set[int]:
        used_pins: set[int] = set()
        for subentry in self._get_entry().subentries.values():
            if CONF_FLOW_PIN_NUMBER in subentry.data:
                used_pins.add(int(subentry.data[CONF_FLOW_PIN_NUMBER]))
        return used_pins

    @callback
    def _available_pins(self, include_pin: int | None = None) -> list[int]:
        used_pins = self._used_pins()
        if include_pin is not None:
            used_pins.discard(include_pin)
        return [pin for pin in range(16) if pin not in used_pins]

    @callback
    def _schema_for_platform(
        self,
        *,
        platform: str,
        available_pins: list[int],
        suggested_data: dict[str, Any] | None = None,
    ) -> vol.Schema:
        defaults = suggested_data or {}
        default_pin = int(defaults.get(CONF_FLOW_PIN_NUMBER, available_pins[0]))
        pin_options = [
            selector.SelectOptionDict(value=pin, label=str(pin)) for pin in available_pins
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_FLOW_PIN_NUMBER,
                    default=default_pin,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=pin_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_FLOW_PIN_NAME,
                    default=defaults.get(CONF_FLOW_PIN_NAME, ""),
                ): str,
                vol.Optional(
                    CONF_INVERT_LOGIC,
                    default=defaults.get(CONF_INVERT_LOGIC, DEFAULT_INVERT_LOGIC),
                ): bool,
            }
        )
        if platform == "binary_sensor":
            schema = schema.extend(
                {
                    vol.Optional(
                        CONF_PULL_MODE,
                        default=_normalize_pull_mode(
                            defaults.get(CONF_PULL_MODE, DEFAULT_PULL_MODE)
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[PULL_MODE_UP, PULL_MODE_NONE],
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key="pull_mode",
                        )
                    ),
                }
            )
        else:
            schema = schema.extend(
                {
                    vol.Optional(
                        CONF_HW_SYNC,
                        default=defaults.get(CONF_HW_SYNC, DEFAULT_HW_SYNC),
                    ): bool,
                    vol.Optional(
                        CONF_MOMENTARY,
                        default=defaults.get(CONF_MOMENTARY, DEFAULT_MOMENTARY),
                    ): bool,
                    vol.Optional(
                        CONF_PULSE_TIME,
                        default=defaults.get(CONF_PULSE_TIME, DEFAULT_PULSE_TIME),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0)),
                }
            )
        return schema

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> SubentryFlowResult:
        """Select pin platform type."""
        if not self._available_pins():
            return self.async_abort(reason="all_pins_configured")
        return self.async_show_menu(menu_options=list(PIN_PLATFORMS))

    async def async_step_binary_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add binary sensor subentry."""
        return await self._async_step_platform("binary_sensor", user_input)

    async def async_step_switch(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add switch subentry."""
        return await self._async_step_platform("switch", user_input)

    async def _async_step_platform(
        self,
        platform: str,
        user_input: dict[str, Any] | None,
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        available_pins = self._available_pins()
        if not available_pins:
            return self.async_abort(reason="all_pins_configured")

        errors: dict[str, str] = {}
        schema = self._schema_for_platform(
            platform=platform,
            available_pins=available_pins,
            suggested_data=user_input,
        )

        if user_input is not None:
            pin_number = int(user_input[CONF_FLOW_PIN_NUMBER])
            if pin_number not in available_pins:
                errors[CONF_FLOW_PIN_NUMBER] = "pin_in_use"
            else:
                data = _build_subentry_data(
                    platform=platform,
                    user_input=user_input,
                    i2c_bus=int(entry.data[CONF_I2C_BUS]),
                    i2c_address=int(entry.data[CONF_I2C_ADDRESS]),
                )
                return self.async_create_entry(
                    title=_pin_title(data),
                    data=data,
                    unique_id=_pin_unique_id(platform, pin_number),
                )

        return self.async_show_form(
            step_id=platform,
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Reconfigure an existing pin subentry."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        platform = str(subentry.data[CONF_FLOW_PLATFORM])
        current_pin = int(subentry.data[CONF_FLOW_PIN_NUMBER])
        available_pins = self._available_pins(include_pin=current_pin)
        errors: dict[str, str] = {}
        suggested = user_input or dict(subentry.data)
        schema = self._schema_for_platform(
            platform=platform,
            available_pins=available_pins,
            suggested_data=suggested,
        )
        if user_input is not None:
            pin_number = int(user_input[CONF_FLOW_PIN_NUMBER])
            if pin_number not in available_pins:
                errors[CONF_FLOW_PIN_NUMBER] = "pin_in_use"
            else:
                data = _build_subentry_data(
                    platform=platform,
                    user_input=user_input,
                    i2c_bus=int(entry.data[CONF_I2C_BUS]),
                    i2c_address=int(entry.data[CONF_I2C_ADDRESS]),
                )
                return self.async_update_and_abort(
                    entry=entry,
                    subentry=subentry,
                    data=data,
                    title=_pin_title(data),
                    unique_id=_pin_unique_id(platform, pin_number),
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
        )


class Mcp23017ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """MCP23017 config flow."""

    VERSION = 4
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: ConfigEntry,  # noqa: ARG003
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return supported subentry flow types."""
        return {SUBENTRY_TYPE_PIN: Mcp23017PinSubentryFlowHandler}

    @callback
    def _chip_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_I2C_BUS, default=DEFAULT_I2C_BUS): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=9)
                ),
                vol.Required(CONF_I2C_ADDRESS, default=DEFAULT_I2C_ADDRESS): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=127)
                ),
                vol.Required(CONF_SCAN_RATE, default=DEFAULT_SCAN_RATE): vol.All(
                    vol.Coerce(float), vol.Range(min=MIN_SCAN_RATE)
                ),
            }
        )

    async def _async_validate_i2c(self, i2c_bus: int, i2c_address: int) -> bool:
        lock, _ = get_i2c_bus_lock(self.hass, DEFAULT_I2C_LOCKS_KEY, i2c_bus)
        async with lock:
            return await self.hass.async_add_executor_job(
                i2c_device_exist,
                i2c_bus,
                i2c_address,
            )

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Create one chip entry from UI."""
        errors: dict[str, str] = {}

        if user_input is not None:
            i2c_bus = int(user_input[CONF_I2C_BUS])
            i2c_address = int(user_input[CONF_I2C_ADDRESS])
            scan_rate = _normalize_scan_rate(user_input.get(CONF_SCAN_RATE))
            unique_id = _chip_unique_id(i2c_bus, i2c_address)
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            if not await self._async_validate_i2c(i2c_bus, i2c_address):
                return self.async_abort(reason="invalid_i2c_address")

            return self.async_create_entry(
                title=_chip_title(i2c_bus, i2c_address),
                data={
                    CONF_I2C_BUS: i2c_bus,
                    CONF_I2C_ADDRESS: i2c_address,
                    CONF_SCAN_RATE: scan_rate,
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=self._chip_schema(),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure chip-level settings."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            i2c_bus = int(user_input[CONF_I2C_BUS])
            i2c_address = int(user_input[CONF_I2C_ADDRESS])
            scan_rate = _normalize_scan_rate(user_input.get(CONF_SCAN_RATE))
            unique_id = _chip_unique_id(i2c_bus, i2c_address)
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_mismatch()

            if not await self._async_validate_i2c(i2c_bus, i2c_address):
                errors[CONF_I2C_ADDRESS] = "invalid_i2c_address"
            else:
                return self.async_update_reload_and_abort(
                    entry=entry,
                    title=_chip_title(i2c_bus, i2c_address),
                    data_updates={
                        CONF_I2C_BUS: i2c_bus,
                        CONF_I2C_ADDRESS: i2c_address,
                        CONF_SCAN_RATE: scan_rate,
                    },
                    unique_id=unique_id,
                )

        schema = self.add_suggested_values_to_schema(self._chip_schema(), dict(entry.data))
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_import(self, user_input: dict[str, Any] | None = None):
        """Import YAML config and translate it to chip + pin subentries."""
        if user_input is None:
            return self.async_abort(reason="invalid_i2c_address")

        i2c_bus = int(user_input.get(CONF_I2C_BUS, DEFAULT_I2C_BUS))
        i2c_address = int(user_input[CONF_I2C_ADDRESS])
        scan_rate = _normalize_scan_rate(user_input.get(CONF_SCAN_RATE, DEFAULT_SCAN_RATE))

        platform = str(user_input[CONF_FLOW_PLATFORM])
        subentry_data: list[dict[str, Any]] = []
        if CONF_PINS in user_input:
            for pin_number, pin_name in user_input[CONF_PINS].items():
                data = _build_subentry_data(
                    platform=platform,
                    user_input={
                        CONF_FLOW_PIN_NUMBER: int(pin_number),
                        CONF_FLOW_PIN_NAME: pin_name,
                        CONF_INVERT_LOGIC: user_input.get(
                            CONF_INVERT_LOGIC, DEFAULT_INVERT_LOGIC
                        ),
                        CONF_PULL_MODE: user_input.get(CONF_PULL_MODE, DEFAULT_PULL_MODE),
                        CONF_HW_SYNC: user_input.get(CONF_HW_SYNC, DEFAULT_HW_SYNC),
                        CONF_MOMENTARY: user_input.get(CONF_MOMENTARY, DEFAULT_MOMENTARY),
                        CONF_PULSE_TIME: user_input.get(
                            CONF_PULSE_TIME, DEFAULT_PULSE_TIME
                        ),
                    },
                    i2c_bus=i2c_bus,
                    i2c_address=i2c_address,
                )
                subentry_data.append(data)
        else:
            subentry_data.append(
                _build_subentry_data(
                    platform=platform,
                    user_input=user_input,
                    i2c_bus=i2c_bus,
                    i2c_address=i2c_address,
                )
            )

        unique_id = _chip_unique_id(i2c_bus, i2c_address)
        existing_entry = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN,
            unique_id,
        )

        if existing_entry is not None:
            for data in subentry_data:
                pin_unique_id = _pin_unique_id(
                    str(data[CONF_FLOW_PLATFORM]),
                    int(data[CONF_FLOW_PIN_NUMBER]),
                )
                if any(
                    sub.unique_id == pin_unique_id
                    or int(sub.data.get(CONF_FLOW_PIN_NUMBER, -1))
                    == int(data[CONF_FLOW_PIN_NUMBER])
                    for sub in existing_entry.subentries.values()
                ):
                    continue
                try:
                    self.hass.config_entries.async_add_subentry(
                        existing_entry,
                        ConfigSubentry(
                            data=MappingProxyType(data),
                            subentry_type=SUBENTRY_TYPE_PIN,
                            title=_pin_title(data),
                            unique_id=pin_unique_id,
                        ),
                    )
                except data_entry_flow.AbortFlow:
                    continue
            return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=_chip_title(i2c_bus, i2c_address),
            data={
                CONF_I2C_BUS: i2c_bus,
                CONF_I2C_ADDRESS: i2c_address,
                CONF_SCAN_RATE: scan_rate,
                CONF_IMPORT_SUBENTRIES: subentry_data,
            },
        )
