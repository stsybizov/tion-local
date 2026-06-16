---
name: Bug report
about: Report a problem with the Tion Local integration
title: ''
labels: bug
assignees: ''
---

## Environment
- **Home Assistant version**:
- **Install type**: <!-- HA OS / Container / Core / Supervised -->
- **Tion Local version**:
- **Breezer model**: <!-- S4 -->
- **Bluetooth**: <!-- built-in adapter / USB dongle / ESP32 bluetooth_proxy -->
- [ ] The Tion phone app was disconnected when the problem occurred (BLE allows one connection)

## Description
<!-- What happens, and what you expected instead. -->

## Steps to reproduce
1.
2.

## Debug log
<!-- Enable debug logging and paste the relevant part of home-assistant.log -->
```yaml
logger:
  default: warning
  logs:
    custom_components.ha_tion_btle: debug
```

```
(log here)
```
