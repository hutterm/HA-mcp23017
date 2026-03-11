"""Platform for MCP23017 switch entities."""

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.event import async_call_later

from . import async_get_component, get_entry_config
from .const import (
    CONF_HW_SYNC,
    CONF_I2C_ADDRESS,
    CONF_I2C_BUS,
    CONF_INVERT_LOGIC,
    CONF_MOMENTARY,
    CONF_PIN_CONFIGS,
    CONF_PIN_MODE,
    CONF_PULSE_TIME,
    DOMAIN,
    PIN_MODE_OUTPUT,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up MCP23017 switches from one chip entry."""
    component = async_get_component(hass, config_entry)
    if component is None:
        _LOGGER.warning("No MCP23017 component found for entry %s", config_entry.entry_id)
        return

    entry_config = get_entry_config(config_entry)
    entities: list[MCP23017Switch] = []
    for pin_number, pin_config in enumerate(entry_config[CONF_PIN_CONFIGS]):
        if pin_config[CONF_PIN_MODE] != PIN_MODE_OUTPUT:
            continue

        entity = MCP23017Switch(
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


class MCP23017Switch(SwitchEntity):
    """Represent one MCP23017 output pin."""

    _attr_has_entity_name = True

    def __init__(
        self,
        component,
        i2c_bus: int,
        i2c_address: int,
        pin_number: int,
        pin_config: dict,
    ) -> None:
        """Initialize switch pin entity."""
        self._device = component
        self._i2c_address = i2c_address
        self._i2c_bus = i2c_bus
        self._pin_number = pin_number
        self._state = False
        self._turn_off_timer_cancel = None

        self._invert_logic = bool(pin_config[CONF_INVERT_LOGIC])
        self._hw_sync = bool(pin_config[CONF_HW_SYNC])
        self._momentary = bool(pin_config[CONF_MOMENTARY])
        self._pulse_time = int(pin_config[CONF_PULSE_TIME])

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
        """Return true if device is on."""
        return self._state

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
        if self._turn_off_timer_cancel:
            self._turn_off_timer_cancel()
            self._turn_off_timer_cancel = None
        await super().async_will_remove_from_hass()
        await self.hass.async_add_executor_job(
            self._device.unregister_entity,
            self._pin_number,
        )

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

    async def configure_device(self):
        """Configure pin as output switch."""
        await self._device.async_set_input(self._pin_number, False)
        if not self._hw_sync:
            await self._device.async_set_pin_value(
                self._pin_number,
                self._invert_logic,
            )

        value = await self._device.async_get_pin_value(self._pin_number)
        self._state = bool(value ^ self._invert_logic)
        return True
