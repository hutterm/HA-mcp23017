"""Platform for mcp23017-based switch."""

import asyncio
import functools
import logging

import voluptuous as vol

from homeassistant.components.switch import PLATFORM_SCHEMA, SwitchEntity
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.event import async_call_later
import homeassistant.helpers.config_validation as cv

from . import async_get_or_create, setup_entry_status
from .const import (
    CONF_FLOW_PIN_NAME,
    CONF_FLOW_PIN_NUMBER,
    CONF_FLOW_PLATFORM,
    CONF_I2C_ADDRESS,
    CONF_I2C_BUS,
    CONF_INVERT_LOGIC,
    CONF_HW_SYNC,
    CONF_MOMENTARY,
    CONF_PINS,
    CONF_PULSE_TIME,
    DEFAULT_I2C_ADDRESS,
    DEFAULT_I2C_BUS,
    DEFAULT_INVERT_LOGIC,
    DEFAULT_HW_SYNC,
    DEFAULT_MOMENTARY,
    DEFAULT_PULSE_TIME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_SWITCHES_SCHEMA = vol.Schema({cv.positive_int: cv.string})

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_PINS): _SWITCHES_SCHEMA,
        vol.Optional(CONF_INVERT_LOGIC, default=DEFAULT_INVERT_LOGIC): cv.boolean,
        vol.Optional(CONF_HW_SYNC, default=DEFAULT_HW_SYNC): cv.boolean,
        vol.Optional(CONF_MOMENTARY, default=DEFAULT_MOMENTARY): cv.boolean,
        vol.Optional(CONF_PULSE_TIME, default=DEFAULT_PULSE_TIME): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
        vol.Optional(CONF_I2C_ADDRESS, default=DEFAULT_I2C_ADDRESS): vol.Coerce(int),
        vol.Optional(CONF_I2C_BUS, default=DEFAULT_I2C_BUS): vol.Coerce(int),
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up MCP23017 for switch entities."""
    while setup_entry_status.busy():
        await asyncio.sleep(0)

    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_FLOW_PLATFORM: "switch",
            CONF_PINS: config[CONF_PINS],
            CONF_I2C_ADDRESS: config[CONF_I2C_ADDRESS],
            CONF_I2C_BUS: config[CONF_I2C_BUS],
            CONF_INVERT_LOGIC: config[CONF_INVERT_LOGIC],
            CONF_HW_SYNC: config[CONF_HW_SYNC],
            CONF_MOMENTARY: config[CONF_MOMENTARY],
            CONF_PULSE_TIME: config[CONF_PULSE_TIME],
        },
    )


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up MCP23017 switch entities from chip subentries."""
    for subentry_id, subentry in config_entry.subentries.items():
        if subentry.data.get(CONF_FLOW_PLATFORM) != "switch":
            continue

        switch_entity = MCP23017Switch(hass, config_entry, subentry.data)
        switch_entity.device = await async_get_or_create(hass, config_entry, switch_entity)
        if await switch_entity.configure_device():
            async_add_entities([switch_entity], config_subentry_id=subentry_id)


async def async_unload_entry(hass, config_entry):
    """Unload MCP23017 switch entry corresponding to config_entry."""
    _LOGGER.debug("switch unload handled by config entry platform unload")
    return True


class MCP23017Switch(SwitchEntity):
    """Represent a switch that uses MCP23017."""

    def __init__(self, hass, config_entry, subentry_data):
        """Initialize the MCP23017 switch."""
        self._device = None
        self._state = None
        self._turn_off_timer_cancel = None

        self._i2c_address = int(config_entry.data[CONF_I2C_ADDRESS])
        self._i2c_bus = int(config_entry.data[CONF_I2C_BUS])
        self._pin_name = str(subentry_data[CONF_FLOW_PIN_NAME])
        self._pin_number = int(subentry_data[CONF_FLOW_PIN_NUMBER])

        self._invert_logic = bool(subentry_data.get(CONF_INVERT_LOGIC, DEFAULT_INVERT_LOGIC))
        self._hw_sync = bool(subentry_data.get(CONF_HW_SYNC, DEFAULT_HW_SYNC))
        self._momentary = bool(subentry_data.get(CONF_MOMENTARY, DEFAULT_MOMENTARY))
        self._pulse_time = max(
            0,
            int(subentry_data.get(CONF_PULSE_TIME, DEFAULT_PULSE_TIME)),
        )

        self._attr_unique_id = (
            f"{DOMAIN}:{self._i2c_bus}:0x{self._i2c_address:02x}-0x{self._pin_number:02x}"
        )

        _LOGGER.info(
            "%s(pin %d:'%s') created",
            type(self).__name__,
            self._pin_number,
            self._pin_name,
        )

    @property
    def icon(self):
        """Return device icon for this entity."""
        return "mdi:chip"

    @property
    def unique_id(self):
        """Return a unique_id for this entity."""
        return self._attr_unique_id

    @property
    def name(self):
        """Return the name of the switch."""
        return self._pin_name

    @property
    def is_on(self):
        """Return true if device is on."""
        return self._state

    @property
    def pin(self):
        """Return the pin number of the entity."""
        return self._pin_number

    @property
    def address(self):
        """Return the i2c address of the entity."""
        return self._i2c_address

    @property
    def bus(self):
        """Return the i2c bus of the entity."""
        return self._i2c_bus

    @property
    def available(self):
        """Return if entity is available."""
        return self.device is not None

    @property
    def device_info(self):
        """Device info."""
        return {
            "identifiers": {(DOMAIN, self._i2c_bus, self._i2c_address)},
            "manufacturer": "Microchip",
            "model": "MCP23017",
            "entry_type": DeviceEntryType.SERVICE,
        }

    @property
    def device(self):
        """Get device property."""
        return self._device

    @device.setter
    def device(self, value):
        """Set device property."""
        self._device = value

    async def async_turn_on(self, **kwargs):
        """Turn the device on."""
        await self._device.async_set_pin_value(
            self._pin_number,
            not self._invert_logic,
        )
        self._state = True
        self.schedule_update_ha_state()

        if self._momentary:
            if self._turn_off_timer_cancel:
                self._turn_off_timer_cancel()

            async def turn_off_listener(_now):
                await self.async_turn_off()

            self._turn_off_timer_cancel = async_call_later(
                self.hass,
                self._pulse_time / 1000.0,
                turn_off_listener,
            )

    async def async_turn_off(self, **kwargs):
        """Turn the device off."""
        await self._device.async_set_pin_value(
            self._pin_number,
            self._invert_logic,
        )
        self._state = False
        self.schedule_update_ha_state()

        if self._momentary and self._turn_off_timer_cancel:
            self._turn_off_timer_cancel()
            self._turn_off_timer_cancel = None

    async def async_will_remove_from_hass(self):
        """Detach entity from shared MCP23017 component."""
        if self._turn_off_timer_cancel:
            self._turn_off_timer_cancel()
            self._turn_off_timer_cancel = None

        if self._device is not None:
            await self.hass.async_add_executor_job(
                functools.partial(self._device.unregister_entity, self._pin_number)
            )

    def unsubscribe_update_listener(self):
        """Compatibility hook for MCP23017.unregister_entity."""

    async def configure_device(self):
        """Attach instance to a device on the given address and configure it."""
        if self.device:
            if not self._hw_sync:
                await self._device.async_set_pin_value(
                    self._pin_number,
                    self._invert_logic,
                )
            await self._device.async_set_input(self._pin_number, False)
            value = await self._device.async_get_pin_value(self._pin_number)
            self._state = bool(value ^ self._invert_logic)
            return True
        return False
