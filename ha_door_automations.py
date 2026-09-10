#!/usr/bin/env python3
"""Create/replace the overnight door-alert automation in Home Assistant via the config API.

Standalone from the bigbox-monitor automations (ha_automations.py); shares the HA
token in monitor.env. Re-run after editing to push changes; it reloads automations.
"""
import json, os, sys, urllib.request

ENV = os.path.join(os.path.dirname(__file__), "monitor.env")
for line in open(ENV):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)

HA = os.environ["HA_URL"]
TOK = os.environ["HA_TOKEN"]
NOTIFY = "notify.mobile_app_pixel_9_pro"

DOOR_SENSORS = [
    "binary_sensor.front_door_sensor",
    "binary_sensor.kitchen_door_sensor",
    "binary_sensor.living_room_living_room_door_sensor",
    "binary_sensor.indoor_garage_door",
]


def api(method, path, payload=None):
    req = urllib.request.Request(
        HA + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read().decode()


AUTOMATIONS = {
    "overnight_door_alert": {
        "alias": "Door opened overnight (urgent)",
        "description": "Any door sensor opens between 00:00 and 06:00 -> urgent push.",
        "trigger": [
            {
                "platform": "state",
                "entity_id": DOOR_SENSORS,
                "from": "off",
                "to": "on",
            }
        ],
        "condition": [
            {"condition": "time", "after": "00:00:00", "before": "06:00:00"}
        ],
        "action": [
            {
                "service": NOTIFY,
                "data": {
                    "title": "\U0001F6A8 Door opened overnight",
                    "message": (
                        "{{ trigger.to_state.attributes.friendly_name }} opened at "
                        "{{ now().strftime('%-I:%M %p') }}"
                    ),
                    "data": {
                        "ttl": 0,
                        "priority": "high",
                        "importance": "high",
                        "channel": "Overnight Security",
                        "tag": "overnight-door",
                        "color": "#D32F2F",
                        "notification_icon": "mdi:door-open",
                        "sticky": True,
                        "media_stream": "alarm_stream_max",
                    },
                },
            }
        ],
        "mode": "parallel",
        "max": 10,
    },
}

if __name__ == "__main__":
    for aid, cfg in AUTOMATIONS.items():
        st, body = api("POST", f"/api/config/automation/config/{aid}", cfg)
        print(f"{aid:24} {st} {body[:120]}")
    print(api("POST", "/api/services/automation/reload", {}))
