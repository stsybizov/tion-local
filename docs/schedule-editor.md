# Schedule editor (dashboard)

Edit the breezer's on-device schedule from a Lovelace dashboard, using input helpers + two
scripts that call `ha_tion_btle.write_schedule` / `refresh_schedule`. No custom cards (HACS)
required.

A Tion "schedule" is a pair of timer slots (start + stop). There are 6 schedules (indices
0–5 → slot pairs 0/1, 2/3, …, 10/11). Edit one schedule at a time: pick the index, **Load**
its current values into the form, change them, then **Save** back to the breezer.

> Editing requires the BLE link — make sure the Tion phone app is disconnected.

## 1. Helpers (`configuration.yaml`, or Settings → Devices & services → Helpers)

```yaml
input_number:
  tion_sched_index: { name: "Schedule #", min: 1, max: 6, step: 1, mode: box, icon: mdi:numeric }
  tion_sched_fan:   { name: "Fan speed", min: 0, max: 6, step: 1, mode: slider, icon: mdi:fan }
  tion_sched_temp:  { name: "Temperature", min: 0, max: 30, step: 1, mode: box, icon: mdi:thermometer, unit_of_measurement: "°C" }

input_datetime:
  tion_sched_start: { name: "Start", has_date: false, has_time: true }
  tion_sched_end:   { name: "End",   has_date: false, has_time: true }

input_select:
  tion_sched_air:   { name: "Air intake", options: ["outside", "recirculation"], icon: mdi:air-filter }

input_boolean:
  tion_sched_heater:  { name: "Heater", icon: mdi:radiator }
  tion_sched_enabled: { name: "Enabled", icon: mdi:calendar-check }
  tion_d_mon: { name: "Mon" }
  tion_d_tue: { name: "Tue" }
  tion_d_wed: { name: "Wed" }
  tion_d_thu: { name: "Thu" }
  tion_d_fri: { name: "Fri" }
  tion_d_sat: { name: "Sat" }
  tion_d_sun: { name: "Sun" }
```

## 2. Scripts (`configuration.yaml`, `script:`)

```yaml
script:
  tion_schedule_apply:
    alias: "Tion: write schedule"
    icon: mdi:content-save
    sequence:
      - service: ha_tion_btle.write_schedule
        data:
          schedule: "{{ states('input_number.tion_sched_index') | int - 1 }}"
          start: "{{ states('input_datetime.tion_sched_start') }}"
          end: "{{ states('input_datetime.tion_sched_end') }}"
          fan_speed: "{{ states('input_number.tion_sched_fan') | int }}"
          target_temp: "{{ states('input_number.tion_sched_temp') | int }}"
          air: "{{ states('input_select.tion_sched_air') }}"
          heater: "{{ is_state('input_boolean.tion_sched_heater','on') }}"
          enabled: "{{ is_state('input_boolean.tion_sched_enabled','on') }}"
          days: >-
            {% set ns = namespace(l=[]) %}
            {% for d in ['mon','tue','wed','thu','fri','sat','sun'] %}
            {% if is_state('input_boolean.tion_d_'~d,'on') %}{% set ns.l = ns.l + [d] %}{% endif %}
            {% endfor %}
            {{ ns.l | join(',') }}

  tion_schedule_load:
    alias: "Tion: load selected into form"
    icon: mdi:download
    variables:
      idx: "{{ states('input_number.tion_sched_index') | int - 1 }}"
      t: "{{ state_attr('sensor.tion_4s_schedule','timers') or [] }}"
      s: "{{ t[idx*2]   if (idx*2)   < t|length else none }}"
      e: "{{ t[idx*2+1] if (idx*2+1) < t|length else none }}"
      mask: "{{ (s.raw[2:4] | int(base=16)) if s else 0 }}"
    sequence:
      - condition: "{{ s is not none }}"
      - service: input_datetime.set_datetime
        target: { entity_id: input_datetime.tion_sched_start }
        data: { time: "{{ s.time }}:00" }
      - service: input_datetime.set_datetime
        target: { entity_id: input_datetime.tion_sched_end }
        data: { time: "{{ (e.time if e else '00:00') }}:00" }
      - service: input_number.set_value
        target: { entity_id: input_number.tion_sched_fan }
        data: { value: "{{ s.fan_speed }}" }
      - service: input_number.set_value
        target: { entity_id: input_number.tion_sched_temp }
        data: { value: "{{ s.target_temp }}" }
      - service: input_select.select_option
        target: { entity_id: input_select.tion_sched_air }
        data: { option: "{{ s.air }}" }
      - service: "input_boolean.turn_{{ 'on' if s.heater else 'off' }}"
        target: { entity_id: input_boolean.tion_sched_heater }
      - service: "input_boolean.turn_{{ 'on' if s.active else 'off' }}"
        target: { entity_id: input_boolean.tion_sched_enabled }
      - service: "input_boolean.turn_{{ 'on' if mask|bitwise_and(1)  else 'off' }}"
        target: { entity_id: input_boolean.tion_d_mon }
      - service: "input_boolean.turn_{{ 'on' if mask|bitwise_and(2)  else 'off' }}"
        target: { entity_id: input_boolean.tion_d_tue }
      - service: "input_boolean.turn_{{ 'on' if mask|bitwise_and(4)  else 'off' }}"
        target: { entity_id: input_boolean.tion_d_wed }
      - service: "input_boolean.turn_{{ 'on' if mask|bitwise_and(8)  else 'off' }}"
        target: { entity_id: input_boolean.tion_d_thu }
      - service: "input_boolean.turn_{{ 'on' if mask|bitwise_and(16) else 'off' }}"
        target: { entity_id: input_boolean.tion_d_fri }
      - service: "input_boolean.turn_{{ 'on' if mask|bitwise_and(32) else 'off' }}"
        target: { entity_id: input_boolean.tion_d_sat }
      - service: "input_boolean.turn_{{ 'on' if mask|bitwise_and(64) else 'off' }}"
        target: { entity_id: input_boolean.tion_d_sun }
```

## 3. Dashboard card

```yaml
type: vertical-stack
cards:
  - type: markdown
    content: >-
      ### 🗓️ Schedule — active: {{ states('sensor.tion_4s_schedule') }}

      | Days | Interval | Spd | Air | Heater |
      |:--|:--:|:--:|:--:|:--:|
      {% set t = state_attr('sensor.tion_4s_schedule','timers') or [] -%}
      {% for i in range(0, t|length, 2) -%}
      {%- set s = t[i] -%}{%- set e = t[i+1] if i+1 < t|length else none -%}
      {%- if s.active %}
      | {{ s.days }} | {{ s.time }}–{{ e.time if e else '?' }} | {{ s.fan_speed }} | {{ '♻️' if s.air=='recirculation' else '🌬️' }} | {{ ('🔥'~s.target_temp~'°') if s.heater else '—' }} |
      {%- endif -%}{%- endfor %}
  - type: entities
    title: Schedule editor
    show_header_toggle: false
    entities:
      - entity: input_number.tion_sched_index
      - type: button
        name: "Load selected #"
        icon: mdi:download
        action_name: Load
        tap_action: { action: call-service, service: script.tion_schedule_load }
      - type: section
        label: Days
      - input_boolean.tion_d_mon
      - input_boolean.tion_d_tue
      - input_boolean.tion_d_wed
      - input_boolean.tion_d_thu
      - input_boolean.tion_d_fri
      - input_boolean.tion_d_sat
      - input_boolean.tion_d_sun
      - type: section
        label: Parameters
      - input_datetime.tion_sched_start
      - input_datetime.tion_sched_end
      - input_number.tion_sched_fan
      - input_select.tion_sched_air
      - input_boolean.tion_sched_heater
      - input_number.tion_sched_temp
      - input_boolean.tion_sched_enabled
  - type: horizontal-stack
    cards:
      - type: button
        name: Save to breezer
        icon: mdi:content-save
        tap_action: { action: call-service, service: script.tion_schedule_apply }
      - type: button
        name: Refresh from breezer
        icon: mdi:calendar-refresh
        tap_action: { action: call-service, service: ha_tion_btle.refresh_schedule }
```
