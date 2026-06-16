![minimum HA version](https://img.shields.io/badge/minimum%20HA%20version-2024.1-blue)
![HACS custom](https://img.shields.io/badge/HACS-Custom-41BDF5)

# Tion Local

🇬🇧 English · [🇷🇺 Русский](README.ru.md)

Local **Bluetooth (BLE)** control of a **Tion 4S** breezer in [Home Assistant](https://www.home-assistant.io/) — no cloud, no MagicAir module, no Tion account. Home Assistant talks to the breezer directly over Bluetooth Low Energy.

> [!WARNING]
> Independent community project. **Not affiliated with, endorsed by, or supported by Tion / TION LLC.** A breezer is a ventilation device, not a room heater — don't rely on it for heating. Everything you do is at your own risk.

This is a standalone fork focused on the **Tion 4S**, with extended telemetry, native turbo and on-device schedule support reverse-engineered on top of the original integration (see [Credits](#credits)). The S3/Lite code paths from the upstream project are retained but only the 4S is actively developed and tested here.

## Features

- **Climate** entity — on/off, `fan_only` / `heat`, target temperature (0–25 °C), presets (Normal / Turbo / Sleep / Away)
- **Fan** entity — on/off + 6 speeds, and a discrete **speed** select (1–6)
- **Air intake** select — outside / recirculation
- **Switches** — sound, backlight, and an "app access" switch that releases the BLE link so the Tion phone app can connect
- **Native Turbo** — runs the breezer's own timed turbo (5 / 10 / 15 min) via a button + duration select; auto-returns to the previous speed
- **On-device schedule** — read and write all 12 timers (6 start/stop schedules) over BLE, with a `write_schedule` service and an optional dashboard editor
- **Sensors** — input/output temperature, current speed, filter life, firmware, control/power board temperatures, **heater power**, **fan power**, **total power**, **energy** (kWh, Energy-dashboard ready), **productivity** (m³/h), **air passed** (m³)
- **Problem** binary sensor with **decoded error/warning codes** (EC01–EC11, WS01–WS06)
- Reliable BLE layer (bleak-retry-connector), reload from the UI, non-blocking startup

## Requirements

- Home Assistant with a working **Bluetooth** integration (built-in adapter, USB dongle, or an **ESP32 `bluetooth_proxy`** near the breezer — recommended if the breezer is far from the host)
- A **Tion 4S** breezer
- The Tion phone app must be **disconnected** while Home Assistant is connected — BLE allows a single connection at a time

## Installation

### HACS (recommended)

1. HACS → ⋮ (top-right) → **Custom repositories**
2. Add `https://github.com/stsybizov/tion-local` with category **Integration**
3. Find **Tion Local** in HACS and install it
4. Restart Home Assistant

### Manual

Copy `custom_components/ha_tion_btle/` into your Home Assistant `config/custom_components/` folder and restart.

## Configuration

1. Settings → **Devices & services** → **Add integration** → search **Tion Local**
2. Fill in the fields:
   - **Model** — `S4`
   - **MAC** — the breezer's Bluetooth address, `UPPERCASE` with colons, e.g. `AA:BB:CC:DD:EE:FF`
   - **Pairing** — see the note below
3. Follow the prompts and finish.

### Pairing note

The 4S uses *Just Works* BLE pairing with a very short window. If the in-flow pairing step fails with an authentication error, bond the device **out-of-band** once (e.g. via `bluetoothctl pair <MAC>` on the host while the breezer is in pairing mode) and then add the integration with **pairing disabled** — Home Assistant will connect over the existing OS bond.

## Entities

| Entity | Notes |
|---|---|
| `climate.tion_4s` | Main control: mode, target temp, fan speed, presets |
| `fan.…_fan_speed` | On/off + 6 speeds |
| `select.tion_4s_air_mode` | Outside / Recirculation |
| `select.tion_4s_speed` | Discrete speed 1–6 |
| `select.tion_4s_boost_duration` | Turbo duration 5 / 10 / 15 min |
| `number.tion_4s_target_temperature` | Heater target, 0–25 °C |
| `switch.tion_4s_sound` / `…_backlight` | Buzzer / LED |
| `switch.tion_4s_app_access_release_ble` | Release BLE for the Tion app |
| `button.tion_4s_boost` | Start native Turbo |
| `button.tion_4s_reset_filter_life` | Reset filter counter (2-press confirm) |
| `binary_sensor.tion_4s_problem` | On if errors≠0; `problems` attribute lists decoded codes |
| `sensor.tion_4s_*` | Temps, filter life, firmware, power/energy, productivity, air passed, schedule |

## Services

| Service | What it does |
|---|---|
| `ha_tion_btle.write_schedule` | Write one of 6 schedules (days, start/end, speed, air, heater) — writes both start+stop timer slots |
| `ha_tion_btle.write_timer` | Write a single raw timer slot (0–11) — advanced |
| `ha_tion_btle.refresh_schedule` | Re-read all 12 timers from the device |
| `ha_tion_btle.set_turbo` | Run native turbo for N seconds |
| `ha_tion_btle.set_timers_enabled` | Enable/disable all schedule timers (advanced) |
| `ha_tion_btle.read_turbo` | Diagnostics: turbo state + raw state frame (returns data) |
| `ha_tion_btle.set_air_source` | Set the air intake on the climate entity |

Standard `climate.*`, `fan.*`, `select.*`, `switch.*`, `number.*` and `button.*` services work on the entities above, so everything is automatable.

### Schedule editor (optional)

The on-device schedule (day/night intervals, fan speed, air mode, heater) can be edited from a Lovelace dashboard using input helpers + a small script that calls `write_schedule`. A ready-to-paste example lives in [`docs/schedule-editor.md`](docs/schedule-editor.md).

> A Tion "schedule" is stored as a pair of timer slots (start + stop). The integration shows only *active* schedules (a timer is active when it is enabled **and** has at least one weekday selected — the app "deletes" a schedule by clearing its days).

## Notes on Turbo / BLE

- Native turbo runs on the device's own timer and returns to the previous (scheduled) speed; it works alongside the on-device schedule.
- BLE is a **single connection** — while the Tion app is connected, Home Assistant can't talk to the breezer, and vice-versa. Use the *app access* switch to hand the link over.
- A weak BLE link can occasionally drop a poll; an **ESP32 bluetooth proxy** near the breezer makes it rock-solid.

## Troubleshooting

- **Entities `unavailable` after a restart** — the breezer reconnects on the next poll; if it persists, check the breezer is on and in range (or add a Bluetooth proxy).
- **Can't edit the schedule from the Tion app** — turn on `switch.tion_4s_app_access_release_ble` first so HA releases the link.
- **Apply changes without a full restart** — use **Reload** in the integration's ⋮ menu.
- Debug logging:
  ```yaml
  logger:
    default: warning
    logs:
      custom_components.ha_tion_btle: debug
  ```

## Credits

This project builds on the work of others:

- **[TionAPI/HA-tion](https://github.com/TionAPI/HA-tion)** by [@IATkachenko](https://github.com/IATkachenko) — the original Home Assistant Tion integration this fork is based on, and the bundled `tion_btle` library.
- **[dentra/esphome-tion](https://github.com/dentra/esphome-tion)** by [@dentra](https://github.com/dentra) — the reference for the Tion 4S BLE protocol (state frame, timers, turbo, error/warning codes) that made the extended 4S features possible.

Huge thanks to both projects and their contributors.

## License

[Apache License 2.0](LICENSE) — same as the upstream project. See [`NOTICE`](NOTICE) for attribution.
