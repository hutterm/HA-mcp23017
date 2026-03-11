# HA-mcp23017

MCP23008/MCP23017 implementation for Home Assistant (HA).

## Highlights

- Async + lock-safe I2C access
- One config entry per chip (one I2C bus + one address)
- Bus dropdown from discovered `/dev/i2c-*` buses
- Address dropdown with MCP23017 address range in hex (`0x20..0x27`)
- Per-chip scan rate
- Per-chip bank polling toggles:
  - bank A (pins 0-7)
  - bank B (pins 8-15)
- Per-pin configuration for all 16 pins:
  - mode: input (binary sensor) / output (switch)
  - invert logic
  - input pull mode
  - output hw sync
  - output momentary + pulse time

## Installation

1. Install with [HACS](https://hacs.xyz/) (custom repository), or copy `custom_components/mcp23017` into your HA config folder.
2. Restart Home Assistant.
3. Add the integration from **Settings → Devices & Services → Add Integration**.
4. Create one entry per MCP23017 chip and configure all 16 pins in the flow.
