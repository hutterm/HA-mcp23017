# Copilot Instructions for HA-mcp23017

## Build, test, and lint commands
- This repository does not define local build/lint/test scripts (no `pyproject.toml`, `tox.ini`, or test files).
- CI validation is defined in:
  - `.github/workflows/validate-hacs.yml` using `hacs/action@main`
  - `.github/workflows/validate-with-hassfest.yaml` using `home-assistant/actions/hassfest@master`
- Single-test command: not available in this repository because no test suite exists.

## High-level architecture
- The integration lives in `custom_components/mcp23017` with domain `mcp23017`.
- One config entry maps to one physical chip (`i2c_bus` + `i2c_address`), and pin behavior is stored in a 16-item `pin_configs` list on that entry.
- `__init__.py` binds one config entry to one MCP23017 device instance (`domain_id = "<bus>:0x<address>"`) and forwards platforms (`binary_sensor`, `switch`) from that single chip entry.
- `MCP23017` in `__init__.py` handles register-level access, cache/state, one poll task per chip, and push updates to registered entities.
- `config_flow.py` uses a chip-centric multi-step flow: general chip settings, then pin bank A (0-7), then pin bank B (8-15). The options flow mirrors this structure.
- `binary_sensor.py` and `switch.py` create entities by iterating each chip entry’s `pin_configs` and selecting pins by `pin_mode`.
- `i2c_lock.py` provides a shared per-bus async lock (stored in `hass.data`) with lock-wait telemetry, serializing SMBus access across chips on the same bus.

## Key codebase conventions
- Keep unique ID formats stable:
  - config-entry unique id: `mcp23017.<bus>.<address>`
  - shared device id: `"<bus>:0x<address>"`
  - component unique id: `"mcp23017:<bus>:0x<address>"`
  - entity unique id suffix: `-0x<pin_hex>`
- Keep pin configuration canonical with `default_pin_config()`, `normalize_pin_config()`, and `normalize_pin_configs()` in `__init__.py`.
- Do SMBus access through `MCP23017` async wrappers (`_async_i2c_call`, `async_set_*`, `async_get_*`) so per-bus locking and executor offloading remain consistent.
- Preserve pull-mode normalization as lowercase enum values (`"up"`, `"none"`). `normalize_pull_mode()` and `async_migrate_entry()` handle legacy uppercase values (`"UP"`, `"NONE"`).
- Keep config/options and translation surfaces in sync when adding/changing pin options:
  - `config_flow.py` schemas and bank input application
  - `custom_components/mcp23017/strings.json`
  - `custom_components/mcp23017/translations/en.json` (and other language files)
- Keep lifecycle coupling intact:
  - Use `MCP23017.domain_id(i2c_bus, i2c_address)` for shared chip identity keys.
  - Keep `unregister_entity()` behavior aligned with unload logic (`has_no_entities` triggers component cleanup).
