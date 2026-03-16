"""Platform for mcp23017-based binary_sensor."""

import asyncio
import functools
import logging

import voluptuous as vol

from homeassistant.components.binary_sensor import PLATFORM_SCHEMA, BinarySensorEntity
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType
import homeassistant.helpers.config_validation as cv

from . import async_get_or_create, setup_entry_status
from .const import (
    CONF_FLOW_PIN_NAME,
    CONF_FLOW_PIN_NUMBER,
    CONF_FLOW_PLATFORM,
    CONF_I2C_ADDRESS,
    CONF_I2C_BUS,
    CONF_INVERT_LOGIC,
    CONF_PINS,
    CONF_PULL_MODE,
    DEFAULT_I2C_ADDRESS,
    DEFAULT_I2C_BUS,
    DEFAULT_INVERT_LOGIC,
    DEFAULT_PULL_MODE,
    DOMAIN,
    PULL_MODE_NONE,
    PULL_MODE_UP,
)

_LOGGER = logging.getLogger(__name__)

_PIN_SCHEMA = vol.Schema({cv.positive_int: cv.string})

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_PINS): _PIN_SCHEMA,
        vol.Optional(CONF_INVERT_LOGIC, default=DEFAULT_INVERT_LOGIC): cv.boolean,
        vol.Optional(CONF_PULL_MODE, default=DEFAULT_PULL_MODE): vol.All(
            vol.Lower, vol.In([PULL_MODE_UP, PULL_MODE_NONE])
        ),
        vol.Optional(CONF_I2C_ADDRESS, default=DEFAULT_I2C_ADDRESS): vol.Coerce(int),
        vol.Optional(CONF_I2C_BUS, default=DEFAULT_I2C_BUS): vol.Coerce(int),
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the MCP23017 platform for binary_sensor entities."""
    while setup_entry_status.busy():
        await asyncio.sleep(0)

    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_FLOW_PLATFORM: "binary_sensor",
            CONF_PINS: config[CONF_PINS],
            CONF_I2C_ADDRESS: config[CONF_I2C_ADDRESS],
            CONF_I2C_BUS: config[CONF_I2C_BUS],
            CONF_INVERT_LOGIC: config[CONF_INVERT_LOGIC],
            CONF_PULL_MODE: config[CONF_PULL_MODE],
        },
    )


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up MCP23017 binary_sensor entities from chip subentries."""
    for subentry_id, subentry in config_entry.subentries.items():
        if subentry.data.get(CONF_FLOW_PLATFORM) != "binary_sensor":
            continue

        binary_sensor_entity = MCP23017BinarySensor(hass, config_entry, subentry.data)
        binary_sensor_entity.device = await async_get_or_create(
            hass,
            config_entry,
            binary_sensor_entity,
        )
        if await binary_sensor_entity.configure_device():
            async_add_entities([binary_sensor_entity], config_subentry_id=subentry_id)


async def async_unload_entry(hass, config_entry):
    """Unload MCP23017 binary_sensor entry corresponding to config_entry."""
    _LOGGER.debug("binary_sensor unload handled by config entry platform unload")
    return True


class MCP23017BinarySensor(BinarySensorEntity):
    """Represent a binary sensor that uses MCP23017."""

    def __init__(self, hass, config_entry, subentry_data):
        """Initialize the MCP23017 binary sensor."""
        self._state = None
        self._device = None

        self._i2c_address = int(config_entry.data[CONF_I2C_ADDRESS])
        self._i2c_bus = int(config_entry.data[CONF_I2C_BUS])
        self._pin_name = str(subentry_data[CONF_FLOW_PIN_NAME])
        self._pin_number = int(subentry_data[CONF_FLOW_PIN_NUMBER])
        self._invert_logic = bool(subentry_data.get(CONF_INVERT_LOGIC, DEFAULT_INVERT_LOGIC))
        self._pull_mode = str(subentry_data.get(CONF_PULL_MODE, DEFAULT_PULL_MODE)).lower()

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
    def should_poll(self):
        """No polling needed from Home Assistant for this entity."""
        return False

    @property
    def name(self):
        """Return the name of the entity."""
        return self._pin_name

    @property
    def is_on(self):
        """Return the state of the entity."""
        return None if self._state is None else self._state != self._invert_logic

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
    def device_info(self):
        """Device info."""
        return {
            "identifiers": {(DOMAIN, self._i2c_bus, self._i2c_address)},
            "manufacturer": "Microchip",
            "model": "MCP23017",
            "entry_type": DeviceEntryType.SERVICE,
        }

    @property
    def available(self):
        """Return if entity is available."""
        return self.device is not None

    @property
    def device(self):
        """Get device property."""
        return self._device

    @device.setter
    def device(self, value):
        """Set device property."""
        self._device = value

    @callback
    async def async_push_update(self, state):
        """Update the GPIO state."""
        self._state = state
        self.async_schedule_update_ha_state()

    async def async_will_remove_from_hass(self):
        """Detach entity from shared MCP23017 component."""
        if self._device is not None:
            await self.hass.async_add_executor_job(
                functools.partial(self._device.unregister_entity, self._pin_number)
            )

    def unsubscribe_update_listener(self):
        """Compatibility hook for MCP23017.unregister_entity."""

    def push_update(self, state):
        """Signal a state change and call the async counterpart."""
        asyncio.run_coroutine_threadsafe(self.async_push_update(state), self.hass.loop)

    async def configure_device(self):
        """Attach instance to a device on the given address and configure it."""
        if self.device:
            await self._device.async_set_input(self._pin_number, True)
            await self._device.async_set_pullup(
                self._pin_number,
                self._pull_mode == PULL_MODE_UP,
            )
            return True
        return False
