# HA-mcp23017

MCP23008/MCP23017 implementation for Home Assistant (HA).

## Highlights

- One configuration entry per chip (one I2C bus + one I2C address)
- Bus dropdown from discovered `/dev/i2c-*` buses
- Address dropdown with MCP23017 hex addresses (`0x20..0x27`)
- Per-chip scan rate
- Per-chip polling toggles for bank A (pins 0-7) and bank B (pins 8-15)
- All 16 pins configured in UI with existing per-pin behavior options
- Lock-safe async I2C behavior for shared-bus reliability

## Installation

1. Install this custom integration with HACS or by copying `custom_components/mcp23017`.
2. Restart Home Assistant.
3. Add `MCP23017 Digital I/O Expander` from **Settings → Devices & Services**.
4. Configure one chip entry and define all 16 pins in the flow.
