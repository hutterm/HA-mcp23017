"""Config flow for MCP23017 component."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector

from . import i2c_device_exist
from .const import (
    CONF_FLOW_PIN_NAME,
    CONF_FLOW_PIN_NUMBER,
    CONF_FLOW_PLATFORM,
    CONF_HW_SYNC,
    CONF_I2C_ADDRESS,
    CONF_I2C_BUS,
    CONF_IMPORT_SUBENTRIES,
    CONF_INVERT_LOGIC,
    CONF_MOMENTARY,
    CONF_PINS,
    CONF_PIN_CONFIGS,
    CONF_PULSE_TIME,
    CONF_PULL_MODE,
    CONF_SCAN_RATE,
    DEFAULT_HW_SYNC,
    DEFAULT_I2C_ADDRESS,
    DEFAULT_I2C_BUS,
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


def _pin_subentry_unique_id(platform: str, pin_number: int) -> str:
    return f"pin.{platform}.{pin_number:02d}"


def _default_pin_name(i2c_bus: int, i2c_address: int, pin_number: int) -> str:
    return f"pin {i2c_bus}:0x{i2c_address:02x}:{pin_number}"


def _normalize_scan_rate(scan_rate: Any) -> float:
    try:
        value = float(scan_rate)
    except (TypeError, ValueError):
        return DEFAULT_SCAN_RATE
    return max(MIN_SCAN_RATE, value)


def _entry_for_chip(
    entries: list[ConfigEntry], i2c_bus: int, i2c_address: int
) -> ConfigEntry | None:
    for entry in entries:
        if (
            int(entry.data.get(CONF_I2C_BUS, DEFAULT_I2C_BUS)) == i2c_bus
            and int(entry.data[CONF_I2C_ADDRESS]) == i2c_address
        ):
            return entry
    return None


def _normalize_pull_mode(pull_mode: Any) -> str:
    value = str(pull_mode or PULL_MODE_UP).lower()
    if value not in (PULL_MODE_UP, PULL_MODE_NONE):
        return PULL_MODE_UP
    return value


def _normalize_pin_config(pin_config: dict[str, Any]) -> dict[str, Any]:
    platform = str(pin_config.get(CONF_FLOW_PLATFORM, "binary_sensor"))
    pin_number = int(pin_config[CONF_FLOW_PIN_NUMBER])
    normalized = {
        CONF_FLOW_PLATFORM: platform,
        CONF_FLOW_PIN_NUMBER: pin_number,
        CONF_FLOW_PIN_NAME: str(pin_config.get(CONF_FLOW_PIN_NAME, f"Pin {pin_number}")),
        CONF_INVERT_LOGIC: bool(pin_config.get(CONF_INVERT_LOGIC, False)),
    }
    if platform == "binary_sensor":
        normalized[CONF_PULL_MODE] = _normalize_pull_mode(pin_config.get(CONF_PULL_MODE))
    elif platform == "switch":
        normalized[CONF_HW_SYNC] = bool(pin_config.get(CONF_HW_SYNC, DEFAULT_HW_SYNC))
        normalized[CONF_MOMENTARY] = bool(
            pin_config.get(CONF_MOMENTARY, DEFAULT_MOMENTARY)
        )
        normalized[CONF_PULSE_TIME] = max(
            0, int(pin_config.get(CONF_PULSE_TIME, DEFAULT_PULSE_TIME))
        )
    return normalized


class Mcp23017PinSubentryFlowHandler(ConfigSubentryFlow):
    """Handle MCP23017 pin subentry flow."""

    def _used_pins(self) -> set[int]:
        used = {
            int(subentry.data[CONF_FLOW_PIN_NUMBER])
            for subentry in self._config_entry.subentries.values()
            if CONF_FLOW_PIN_NUMBER in subentry.data
        }
        for pin_config in self._config_entry.data.get(CONF_PIN_CONFIGS, []):
            if CONF_FLOW_PIN_NUMBER in pin_config:
                used.add(int(pin_config[CONF_FLOW_PIN_NUMBER]))
        return used

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        used_pins = self._used_pins()

        if user_input is not None:
            pin_number = int(user_input[CONF_FLOW_PIN_NUMBER])
            if pin_number in used_pins:
                errors[CONF_FLOW_PIN_NUMBER] = "pin_already_configured"
            else:
                return await self.async_step_platform(
                    {
                        CONF_FLOW_PIN_NUMBER: pin_number,
                        CONF_FLOW_PLATFORM: user_input[CONF_FLOW_PLATFORM],
                    }
                )

        available_pins = [pin for pin in range(16) if pin not in used_pins]
        if not available_pins:
            return self.async_abort(reason="all_pins_configured")

        schema = vol.Schema(
            {
                vol.Required(CONF_FLOW_PIN_NUMBER): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=available_pins,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_FLOW_PLATFORM,
                    default="binary_sensor",
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(PIN_PLATFORMS),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_platform(self, user_input: dict[str, Any]):
        """Route to platform-specific configuration step."""
        platform = user_input[CONF_FLOW_PLATFORM]
        if platform == "binary_sensor":
            return await self.async_step_binary_sensor(user_input)
        return await self.async_step_switch(user_input)

    def _base_schema(self, defaults: dict[str, Any], *, include_platform: bool):
        i2c_bus = int(self._config_entry.data.get(CONF_I2C_BUS, DEFAULT_I2C_BUS))
        i2c_address = int(self._config_entry.data[CONF_I2C_ADDRESS])
        pin_number = int(defaults[CONF_FLOW_PIN_NUMBER])
        default_name = _default_pin_name(i2c_bus, i2c_address, pin_number)

        schema: dict[Any, Any] = {}
        if include_platform:
            schema[
                vol.Required(
                    CONF_FLOW_PLATFORM,
                    default=defaults.get(CONF_FLOW_PLATFORM, "binary_sensor"),
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(PIN_PLATFORMS),
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        schema[
            vol.Required(CONF_FLOW_PIN_NUMBER, default=pin_number)
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(range(16)),
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
        schema[
            vol.Required(
                CONF_FLOW_PIN_NAME,
                default=defaults.get(CONF_FLOW_PIN_NAME, default_name),
            )
        ] = cv.string
        schema[
            vol.Required(
                CONF_INVERT_LOGIC,
                default=bool(defaults.get(CONF_INVERT_LOGIC, False)),
            )
        ] = cv.boolean
        return schema

    def _validate_pin_form(
        self, candidate_pin: int, *, current_pin: int | None = None
    ) -> dict[str, str]:
        used = self._used_pins()
        if current_pin is not None:
            used.discard(int(current_pin))
        if candidate_pin in used:
            return {CONF_FLOW_PIN_NUMBER: "pin_already_configured"}
        return {}

    def _get_platform_schema(
        self, platform: str, defaults: dict[str, Any], *, include_platform: bool
    ):
        base_schema = self._base_schema(defaults, include_platform=include_platform)
        if platform == "binary_sensor":
            base_schema[
                vol.Required(
                    CONF_PULL_MODE,
                    default=_normalize_pull_mode(
                        defaults.get(CONF_PULL_MODE, PULL_MODE_UP)
                    ),
                )
            ] = vol.In([PULL_MODE_UP, PULL_MODE_NONE])
        else:
            base_schema[
                vol.Required(
                    CONF_HW_SYNC,
                    default=bool(defaults.get(CONF_HW_SYNC, DEFAULT_HW_SYNC)),
                )
            ] = cv.boolean
            base_schema[
                vol.Required(
                    CONF_MOMENTARY,
                    default=bool(defaults.get(CONF_MOMENTARY, DEFAULT_MOMENTARY)),
                )
            ] = cv.boolean
            base_schema[
                vol.Required(
                    CONF_PULSE_TIME,
                    default=max(
                        0,
                        int(defaults.get(CONF_PULSE_TIME, DEFAULT_PULSE_TIME)),
                    ),
                )
            ] = vol.All(vol.Coerce(int), vol.Range(min=0))
        return vol.Schema(base_schema)

    async def _platform_submit_step(
        self,
        step_id: str,
        *,
        default_data: dict[str, Any],
        user_input: dict[str, Any] | None,
        include_platform: bool,
        current_pin: int | None = None,
    ):
        platform = str(default_data.get(CONF_FLOW_PLATFORM, "binary_sensor"))
        errors: dict[str, str] = {}

        if user_input is not None:
            submitted = dict(user_input)
            platform = str(submitted.get(CONF_FLOW_PLATFORM, platform))
            submitted[CONF_FLOW_PLATFORM] = platform
            submitted[CONF_FLOW_PIN_NUMBER] = int(submitted[CONF_FLOW_PIN_NUMBER])
            submitted[CONF_INVERT_LOGIC] = bool(submitted.get(CONF_INVERT_LOGIC, False))
            submitted[CONF_FLOW_PIN_NAME] = str(submitted[CONF_FLOW_PIN_NAME]).strip()

            if platform == "binary_sensor":
                submitted[CONF_PULL_MODE] = _normalize_pull_mode(
                    submitted.get(CONF_PULL_MODE, PULL_MODE_UP)
                )
                submitted.pop(CONF_HW_SYNC, None)
                submitted.pop(CONF_MOMENTARY, None)
                submitted.pop(CONF_PULSE_TIME, None)
            else:
                submitted[CONF_HW_SYNC] = bool(
                    submitted.get(CONF_HW_SYNC, DEFAULT_HW_SYNC)
                )
                submitted[CONF_MOMENTARY] = bool(
                    submitted.get(CONF_MOMENTARY, DEFAULT_MOMENTARY)
                )
                submitted[CONF_PULSE_TIME] = max(
                    0,
                    int(submitted.get(CONF_PULSE_TIME, DEFAULT_PULSE_TIME)),
                )
                submitted.pop(CONF_PULL_MODE, None)

            errors.update(
                self._validate_pin_form(
                    submitted[CONF_FLOW_PIN_NUMBER], current_pin=current_pin
                )
            )
            if not errors:
                return self.async_create_entry(title="", data=submitted)

            default_data = submitted

        schema = self._get_platform_schema(
            platform, default_data, include_platform=include_platform
        )
        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    async def async_step_binary_sensor(self, user_input: dict[str, Any] | None = None):
        defaults = {
            CONF_FLOW_PLATFORM: "binary_sensor",
            **(user_input or {}),
        }
        return await self._platform_submit_step(
            "binary_sensor",
            default_data=defaults,
            user_input=user_input,
            include_platform=False,
        )

    async def async_step_switch(self, user_input: dict[str, Any] | None = None):
        defaults = {
            CONF_FLOW_PLATFORM: "switch",
            **(user_input or {}),
        }
        return await self._platform_submit_step(
            "switch",
            default_data=defaults,
            user_input=user_input,
            include_platform=False,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = _normalize_pin_config(dict(self._subentry.data))
        platform = str(current.get(CONF_FLOW_PLATFORM, "binary_sensor"))
        current_pin = int(current[CONF_FLOW_PIN_NUMBER])

        if user_input is not None:
            updated = dict(user_input)
            updated[CONF_FLOW_PIN_NUMBER] = int(updated[CONF_FLOW_PIN_NUMBER])
            updated[CONF_INVERT_LOGIC] = bool(updated.get(CONF_INVERT_LOGIC, False))
            updated[CONF_FLOW_PIN_NAME] = str(updated[CONF_FLOW_PIN_NAME]).strip()
            updated[CONF_FLOW_PLATFORM] = str(updated.get(CONF_FLOW_PLATFORM, platform))

            if updated[CONF_FLOW_PLATFORM] == "binary_sensor":
                updated[CONF_PULL_MODE] = _normalize_pull_mode(
                    updated.get(CONF_PULL_MODE, PULL_MODE_UP)
                )
                updated.pop(CONF_HW_SYNC, None)
                updated.pop(CONF_MOMENTARY, None)
                updated.pop(CONF_PULSE_TIME, None)
            else:
                updated[CONF_HW_SYNC] = bool(updated.get(CONF_HW_SYNC, DEFAULT_HW_SYNC))
                updated[CONF_MOMENTARY] = bool(
                    updated.get(CONF_MOMENTARY, DEFAULT_MOMENTARY)
                )
                updated[CONF_PULSE_TIME] = max(
                    0,
                    int(updated.get(CONF_PULSE_TIME, DEFAULT_PULSE_TIME)),
                )
                updated.pop(CONF_PULL_MODE, None)

            errors = self._validate_pin_form(
                updated[CONF_FLOW_PIN_NUMBER], current_pin=current_pin
            )
            if not errors:
                return self.async_update_and_abort(self._subentry, data=updated)

            current = updated
            platform = updated[CONF_FLOW_PLATFORM]
        else:
            errors = {}

        schema = self._get_platform_schema(platform, current, include_platform=True)
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )


class Mcp23017ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MCP23017."""

    VERSION = 6

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get options flow."""
        return Mcp23017OptionsFlowHandler(config_entry)

    @staticmethod
    @callback
    def async_get_supported_subentry_types(
        config_entry: ConfigEntry,
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this config entry."""
        return {SUBENTRY_TYPE_PIN: Mcp23017PinSubentryFlowHandler}

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Import from configuration.yaml."""
        i2c_bus = int(import_data.get(CONF_I2C_BUS, DEFAULT_I2C_BUS))
        i2c_address = int(import_data.get(CONF_I2C_ADDRESS, DEFAULT_I2C_ADDRESS))
        unique_id = _chip_unique_id(i2c_bus, i2c_address)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        pin_platform = str(import_data[CONF_FLOW_PLATFORM])
        pin_defs = import_data.get(CONF_PINS, {})
        subentries_data = [
            _build_pin_config_from_import(
                i2c_bus=i2c_bus,
                i2c_address=i2c_address,
                pin_number=int(pin_number),
                pin_name=str(pin_name),
                platform=pin_platform,
                source_data=import_data,
            )
            for pin_number, pin_name in pin_defs.items()
        ]

        existing_entries = self.hass.config_entries.async_entries(DOMAIN)
        existing_entry = _entry_for_chip(existing_entries, i2c_bus, i2c_address)
        if existing_entry:
            for subentry_data in subentries_data:
                pin_number = int(subentry_data[CONF_FLOW_PIN_NUMBER])
                subentry_unique_id = _pin_subentry_unique_id(
                    subentry_data[CONF_FLOW_PLATFORM], pin_number
                )
                existing_subentry = next(
                    (
                        subentry
                        for subentry in existing_entry.subentries.values()
                        if subentry.unique_id == subentry_unique_id
                        or int(subentry.data.get(CONF_FLOW_PIN_NUMBER, -1)) == pin_number
                    ),
                    None,
                )
                if existing_subentry:
                    continue
                self.hass.config_entries.async_add_subentry(
                    existing_entry,
                    ConfigSubentry(
                        subentry_id="",
                        unique_id=subentry_unique_id,
                        subentry_type=SUBENTRY_TYPE_PIN,
                        title=subentry_data[CONF_FLOW_PIN_NAME],
                        data=subentry_data,
                    ),
                )
            data_updates = {
                key: value
                for key, value in existing_entry.data.items()
                if key
                not in (
                    CONF_FLOW_PLATFORM,
                    CONF_FLOW_PIN_NUMBER,
                    CONF_FLOW_PIN_NAME,
                )
            }
            self.hass.config_entries.async_update_entry(existing_entry, data=data_updates)
            return self.async_abort(reason="already_configured")

        title = f"Bus: {i2c_bus}, address: 0x{i2c_address:02x}"
        return self.async_create_entry(
            title=title,
            data={
                CONF_I2C_BUS: i2c_bus,
                CONF_I2C_ADDRESS: i2c_address,
                CONF_SCAN_RATE: _normalize_scan_rate(import_data.get(CONF_SCAN_RATE)),
                CONF_IMPORT_SUBENTRIES: subentries_data,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            i2c_bus = int(user_input[CONF_I2C_BUS])
            i2c_address = int(user_input[CONF_I2C_ADDRESS])
            unique_id = _chip_unique_id(i2c_bus, i2c_address)

            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            async with get_i2c_bus_lock(self.hass, i2c_bus):
                exists = await self.hass.async_add_executor_job(
                    i2c_device_exist,
                    i2c_bus,
                    i2c_address,
                )
            if not exists:
                errors["base"] = "i2c_device_not_found"
            else:
                return self.async_create_entry(
                    title=f"Bus: {i2c_bus}, address: 0x{i2c_address:02x}",
                    data={
                        CONF_I2C_BUS: i2c_bus,
                        CONF_I2C_ADDRESS: i2c_address,
                        CONF_SCAN_RATE: _normalize_scan_rate(
                            user_input.get(CONF_SCAN_RATE)
                        ),
                    },
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_I2C_BUS, default=DEFAULT_I2C_BUS): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=9,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_I2C_ADDRESS,
                    default=DEFAULT_I2C_ADDRESS,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=127,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_SCAN_RATE, default=DEFAULT_SCAN_RATE): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_RATE,
                        max=10.0,
                        step=0.01,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of chip-level settings."""
        config_entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            i2c_bus = int(user_input[CONF_I2C_BUS])
            i2c_address = int(user_input[CONF_I2C_ADDRESS])
            unique_id = _chip_unique_id(i2c_bus, i2c_address)

            async with get_i2c_bus_lock(self.hass, i2c_bus):
                exists = await self.hass.async_add_executor_job(
                    i2c_device_exist,
                    i2c_bus,
                    i2c_address,
                )
            if not exists:
                errors["base"] = "i2c_device_not_found"
            else:
                existing = _entry_for_chip(
                    [
                        entry
                        for entry in self.hass.config_entries.async_entries(DOMAIN)
                        if entry.entry_id != config_entry.entry_id
                    ],
                    i2c_bus,
                    i2c_address,
                )
                if existing:
                    errors["base"] = "already_configured"
                else:
                    return self.async_update_reload_and_abort(
                        config_entry,
                        unique_id=unique_id,
                        data_updates={
                            CONF_I2C_BUS: i2c_bus,
                            CONF_I2C_ADDRESS: i2c_address,
                            CONF_SCAN_RATE: _normalize_scan_rate(
                                user_input.get(CONF_SCAN_RATE)
                            ),
                        },
                    )

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_I2C_BUS,
                    default=int(config_entry.data.get(CONF_I2C_BUS, DEFAULT_I2C_BUS)),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=9,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_I2C_ADDRESS,
                    default=int(config_entry.data[CONF_I2C_ADDRESS]),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=127,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_SCAN_RATE,
                    default=float(config_entry.data.get(CONF_SCAN_RATE, DEFAULT_SCAN_RATE)),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_RATE,
                        max=10.0,
                        step=0.01,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauth requests."""
        return self.async_abort(reason="reauth_not_supported")

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirm step."""
        return self.async_abort(reason="reauth_not_supported")


def _build_pin_config_from_import(
    *,
    i2c_bus: int,
    i2c_address: int,
    pin_number: int,
    pin_name: str,
    platform: str,
    source_data: dict[str, Any],
) -> dict[str, Any]:
    data: dict[str, Any] = {
        CONF_FLOW_PLATFORM: platform,
        CONF_FLOW_PIN_NUMBER: pin_number,
        CONF_FLOW_PIN_NAME: pin_name
        or _default_pin_name(i2c_bus, i2c_address, pin_number),
        CONF_INVERT_LOGIC: bool(source_data.get(CONF_INVERT_LOGIC, False)),
    }
    if platform == "binary_sensor":
        data[CONF_PULL_MODE] = _normalize_pull_mode(source_data.get(CONF_PULL_MODE))
    else:
        data[CONF_HW_SYNC] = bool(source_data.get(CONF_HW_SYNC, DEFAULT_HW_SYNC))
        data[CONF_MOMENTARY] = bool(source_data.get(CONF_MOMENTARY, DEFAULT_MOMENTARY))
        data[CONF_PULSE_TIME] = max(
            0, int(source_data.get(CONF_PULSE_TIME, DEFAULT_PULSE_TIME))
        )
    return data


class Mcp23017OptionsFlowHandler(config_entries.OptionsFlow):
    """MCP23017 options flow.

    Note: chip-level scan_rate/readout entities are exposed as runtime entities.
    Keep this options flow minimal to preserve existing "Configure" entrypoint.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Return an informational form without editable options."""
        return self.async_show_form(step_id="init", data_schema=vol.Schema({}))

