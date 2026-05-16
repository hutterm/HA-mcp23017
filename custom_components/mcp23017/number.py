"""Platform for MCP23017 chip-level number entities."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from . import async_get_or_create
from .const import (
    CONF_I2C_ADDRESS,
    CONF_I2C_BUS,
    CONF_SCAN_RATE,
    DEFAULT_SCAN_RATE,
    DOMAIN,
)

MIN_SCAN_RATE = 0.01
MAX_SCAN_RATE = 60.0


def _normalize_scan_rate(scan_rate):
    try:
        value = float(scan_rate)
    except (TypeError, ValueError):
        value = DEFAULT_SCAN_RATE
    return max(MIN_SCAN_RATE, min(MAX_SCAN_RATE, value))


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up MCP23017 chip-level number entities."""
    scan_rate_number = MCP23017ScanRateNumber(config_entry)
    scan_rate_number.device = await async_get_or_create(
        hass,
        config_entry,
        scan_rate_number,
        register_entity=False,
    )
    if scan_rate_number.device is not None:
        async_add_entities([scan_rate_number])


async def async_unload_entry(hass, config_entry):
    """Unload MCP23017 number entry corresponding to config_entry."""
    return True


class MCP23017ScanRateNumber(NumberEntity, RestoreEntity):
    """Represent chip-level scan rate entity."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False
    _attr_native_min_value = MIN_SCAN_RATE
    _attr_native_max_value = MAX_SCAN_RATE
    _attr_native_step = 0.01
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX

    def __init__(self, config_entry):
        """Initialize scan rate number entity."""
        self._device = None
        self._i2c_address = int(config_entry.data[CONF_I2C_ADDRESS])
        self._i2c_bus = int(config_entry.data[CONF_I2C_BUS])
        self._native_value = _normalize_scan_rate(
            config_entry.data.get(CONF_SCAN_RATE, DEFAULT_SCAN_RATE)
        )
        self._attr_unique_id = (
            f"{DOMAIN}:{self._i2c_bus}:0x{self._i2c_address:02x}-{CONF_SCAN_RATE}"
        )
        self._attr_name = "Scan rate"

    async def async_added_to_hass(self):
        """Restore last state and apply it to the device."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if (
            last_state is not None
            and last_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
        ):
            self._native_value = _normalize_scan_rate(last_state.state)

        if self._device is not None:
            self._device.scan_rate = self._native_value

    @property
    def icon(self):
        """Return device icon for this entity."""
        return "mdi:chip"

    @property
    def native_value(self):
        """Return the scan rate."""
        if self._device is None:
            return self._native_value
        return self._device.scan_rate

    async def async_set_native_value(self, value: float):
        """Set a new scan rate."""
        self._native_value = _normalize_scan_rate(value)
        if self._device is not None:
            self._device.scan_rate = self._native_value
        self.async_write_ha_state()

    @property
    def available(self):
        """Return if entity is available."""
        return self.device is not None

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
    def device(self):
        """Get device property."""
        return self._device

    @device.setter
    def device(self, value):
        """Set device property."""
        self._device = value
