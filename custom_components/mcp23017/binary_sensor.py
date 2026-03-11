"""Platform for MCP23017 binary sensor entities."""

import asyncio
import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.device_registry import DeviceEntryType

from . import async_get_component, get_entry_config
from .const import (
    CONF_I2C_ADDRESS,
    CONF_I2C_BUS,
    CONF_INVERT_LOGIC,
    CONF_PIN_CONFIGS,
    CONF_PIN_MODE,
    CONF_PULL_MODE,
    DOMAIN,
    MODE_UP,
    PIN_MODE_INPUT,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up MCP23017 binary sensors from one chip entry."""
    component = async_get_component(hass, config_entry)
    if component is None:
        _LOGGER.warning("No MCP23017 component found for entry %s", config_entry.entry_id)
        return

    entry_config = get_entry_config(config_entry)
    entities: list[MCP23017BinarySensor] = []
    for pin_number, pin_config in enumerate(entry_config[CONF_PIN_CONFIGS]):
        if pin_config[CONF_PIN_MODE] != PIN_MODE_INPUT:
            continue

        entity = MCP23017BinarySensor(
            component=component,
            i2c_bus=entry_config[CONF_I2C_BUS],
            i2c_address=entry_config[CONF_I2C_ADDRESS],
            pin_number=pin_number,
            pin_config=pin_config,
        )
        if await entity.configure_device():
            entities.append(entity)

    if entities:
        async_add_entities(entities)


class MCP23017BinarySensor(BinarySensorEntity):
    """Represent one MCP23017 input pin."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        component,
        i2c_bus: int,
        i2c_address: int,
        pin_number: int,
        pin_config: dict,
    ) -> None:
        """Initialize binary sensor pin entity."""
        self._state = False
        self._device = component
        self._i2c_address = i2c_address
        self._i2c_bus = i2c_bus
        self._pin_number = pin_number

        self._invert_logic = bool(pin_config[CONF_INVERT_LOGIC])
        self._pull_mode = pin_config[CONF_PULL_MODE]

        self._attr_name = f"Pin {pin_number}"

    @property
    def unique_id(self):
        """Return a unique_id for this entity."""
        return f"{self._device.unique_id}-0x{self._pin_number:02x}"

    @property
    def icon(self):
        """Return device icon for this entity."""
        return "mdi:chip"

    @property
    def is_on(self):
        """Return the state of the entity."""
        return self._state != self._invert_logic

    @property
    def pin(self):
        """Return the pin number of the entity."""
        return self._pin_number

    @property
    def device_info(self):
        """Device info."""
        return {
            "identifiers": {(DOMAIN, self._i2c_bus, self._i2c_address)},
            "manufacturer": "Microchip",
            "model": "MCP23017",
            "entry_type": DeviceEntryType.SERVICE,
        }

    async def async_added_to_hass(self) -> None:
        """Handle entity added to Home Assistant."""
        await super().async_added_to_hass()
        await self.hass.async_add_executor_job(self._device.register_entity, self)

    async def async_will_remove_from_hass(self) -> None:
        """Handle entity removal from Home Assistant."""
        await super().async_will_remove_from_hass()
        await self.hass.async_add_executor_job(
            self._device.unregister_entity,
            self._pin_number,
        )

    async def async_push_update(self, state):
        """Update the GPIO state."""
        self._state = state
        self.async_schedule_update_ha_state()

    def push_update(self, state):
        """Signal a state change and call the async counterpart."""
        asyncio.run_coroutine_threadsafe(self.async_push_update(state), self.hass.loop)

    async def configure_device(self):
        """Configure pin as binary input."""
        await self._device.async_set_input(self._pin_number, True)
        await self._device.async_set_pullup(
            self._pin_number,
            bool(self._pull_mode == MODE_UP),
        )
        self._state = await self._device.async_get_pin_value(self._pin_number)
        return True
