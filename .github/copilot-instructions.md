# Copilot Instructions for HA-mcp23017

## Build, test, and lint commands
- This repository does not define local build/lint/test scripts (no `pyproject.toml`, `tox.ini`, or test files).
- CI validation is defined in:
  - `.github/workflows/validate-hacs.yml` using `hacs/action@main`
  - `.github/workflows/validate-with-hassfest.yaml` using `home-assistant/actions/hassfest@master`
- Single-test command: not available in this repository because no test suite exists.

## High-level architecture
- The integration lives in `custom_components/mcp23017` with domain `mcp23017`.
- One config entry maps to one pin entity (`platform` + `i2c_bus` + `i2c_address` + `pin_number`), not one full chip.
- `__init__.py` manages shared chip instances per bus/address via `hass.data[DOMAIN][domain_id]` and `async_get_or_create()`, so multiple pin entities reuse one MCP23017 device object.
- `MCP23017` in `__init__.py` handles register-level access, cache/state, one poll task per chip, and push updates to registered entities.
- `config_flow.py` creates per-entity entries (UI/import) and validates bus/address reachability under the shared bus lock.
- `Mcp23017OptionsFlowHandler` configures per-entity behavior:
  - `binary_sensor`: `invert_logic`, `pull_mode`
  - `switch`: `invert_logic`, `hw_sync`, `momentary`, `pulse_time`
- `binary_sensor.py` and `switch.py` both support legacy `configuration.yaml` import via `async_setup_platform`, but runtime management is through config entries.
- `i2c_lock.py` provides a shared per-bus async lock (stored in `hass.data`) with lock-wait telemetry, serializing SMBus access across chips on the same bus.

## Key codebase conventions
- Keep unique ID formats stable:
  - config-entry unique id: `mcp23017.<bus>.<address>.<pin>`
  - shared device id: `"<bus>:0x<address>"`
  - component unique id: `"mcp23017:<bus>:0x<address>"`
  - entity unique id suffix: `-0x<pin_hex>`
- Reuse the shared device path (`async_get_or_create`, `MCP23017_DATA_LOCK`, `register_entity`/`unregister_entity`) instead of creating per-entity SMBus instances.
- Do SMBus access through `MCP23017` async wrappers (`_async_i2c_call`, `async_set_*`, `async_get_*`) so per-bus locking and executor offloading remain consistent.
- Preserve pull-mode normalization as lowercase enum values (`"up"`, `"none"`). Migration in `async_migrate_entry()` maps legacy `"NONE"` to `"none"`.
- Keep config/options and translation surfaces in sync when adding/changing options:
  - `config_flow.py` schemas/selectors
  - `custom_components/mcp23017/strings.json`
  - `custom_components/mcp23017/translations/en.json` (and other language files)
- Keep lifecycle coupling intact:
  - Use `MCP23017.domain_id(i2c_bus, i2c_address)` for shared chip identity keys.
  - Keep `unregister_entity()` behavior aligned with unload logic (`has_no_entities` triggers component cleanup).
