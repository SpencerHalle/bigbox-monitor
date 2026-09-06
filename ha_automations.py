#!/usr/bin/env python3
"""Create/replace the bigbox-monitor automations in Home Assistant via the config API."""
import json, sys, urllib.request

HA = "http://10.0.0.90:8123"
TOK = open("/dev/stdin").read().strip() if not sys.stdin.isatty() else sys.argv[1]
NOTIFY = "notify.mobile_app_pixel_9_pro"

def api(method, path, payload=None):
    req = urllib.request.Request(HA + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"},
        method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read().decode()

def notify(title, message, critical=False):
    data = {"title": title, "message": message}
    if critical:
        data["data"] = {"push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}},
                        "priority": "high", "ttl": 0}
    return {"service": NOTIFY, "data": data}

AUTOMATIONS = {
  "bigbox_disk_full": {
    "alias": "bigbox: disk over 90%",
    "description": "Any bigbox filesystem crosses 90% full.",
    "trigger": [{"platform": "numeric_state", "entity_id": "sensor.bigbox_disk_max",
                 "above": 90, "for": {"minutes": 10}}],
    "action": [notify("⚠️ bigbox disk almost full",
        "{{ state_attr('sensor.bigbox_disk_max','mount') }} is "
        "{{ states('sensor.bigbox_disk_max') }}% full.")],
    "mode": "single",
  },
  "bigbox_smart_failed": {
    "alias": "bigbox: SMART drive failure",
    "description": "A drive reports SMART FAILED.",
    "trigger": [{"platform": "state", "entity_id": "sensor.bigbox_smart", "to": "FAILED"}],
    "action": [notify("🛑 bigbox DRIVE FAILURE",
        "SMART FAILED: {{ dict(state_attr('sensor.bigbox_smart','drives') or {}) "
        "| dictsort | selectattr('1','eq','FAILED') | map(attribute='0') | join(', ') }}. "
        "Check the drive now.", critical=True)],
    "mode": "single",
  },
  "bigbox_zpool_unhealthy": {
    "alias": "bigbox: ZFS pool not ONLINE",
    "description": "tank pool leaves the ONLINE state (DEGRADED/FAULTED/etc).",
    "trigger": [{"platform": "state", "entity_id": "sensor.bigbox_zpool_tank"}],
    "condition": [{"condition": "template",
        "value_template": "{{ states('sensor.bigbox_zpool_tank') not in ['ONLINE','unknown','unavailable'] }}"}],
    "action": [notify("🛑 bigbox ZFS pool " + "{{ states('sensor.bigbox_zpool_tank') }}",
        "tank is {{ states('sensor.bigbox_zpool_tank') }} "
        "({{ state_attr('sensor.bigbox_zpool_tank','capacity_pct') }}% full). Run: zpool status -v",
        critical=True)],
    "mode": "single",
  },
  "bigbox_container_down": {
    "alias": "bigbox: a container is down",
    "description": "One or more podman containers stopped or went unhealthy for 5 min.",
    "trigger": [{"platform": "template",
        "value_template": "{{ (state_attr('sensor.bigbox_containers','down') or []) | length > 0 }}",
        "for": {"minutes": 5}}],
    "action": [notify("⚠️ bigbox container down",
        "Down: {{ (state_attr('sensor.bigbox_containers','down') or []) | join(', ') }}")],
    "mode": "single",
  },
  "bigbox_backup_failed": {
    "alias": "bigbox: nightly backup failed or stale",
    "description": "Local backup to /media/backup failed, or hasn't succeeded in 30h.",
    "trigger": [{"platform": "state", "entity_id": "sensor.bigbox_backup",
                 "to": ["failed", "stale"], "for": {"minutes": 10}}],
    "action": [notify("⚠️ bigbox backup " + "{{ states('sensor.bigbox_backup') }}",
        "Last run {{ state_attr('sensor.bigbox_backup','last_run') }} "
        "({{ state_attr('sensor.bigbox_backup','age_hours') }}h ago). "
        "Steps: {{ state_attr('sensor.bigbox_backup','steps') }}")],
    "mode": "single",
  },
  "bigbox_mc_join": {
    "alias": "SMP: player joined / left",
    "description": "Any change to who is on the SMP — fires on every join and every leave, "
                   "not just the first person on an empty server.",
    "trigger": [{"platform": "state", "entity_id": "sensor.bigbox_mc_players"}],
    "condition": [{"condition": "template", "value_template":
        "{{ trigger.from_state is not none and "
        "(trigger.to_state.attributes.players | default([])) != "
        "(trigger.from_state.attributes.players | default([])) }}"}],
    "action": [notify("SMP",
        "{% set new = trigger.to_state.attributes.players | default([]) %}"
        "{% set old = trigger.from_state.attributes.players | default([]) %}"
        "{% set joined = new | reject('in', old) | list %}"
        "{% set left = old | reject('in', new) | list %}"
        "{% if joined %}🟢 {{ joined | join(', ') }} joined{% endif %}"
        "{% if joined and left %} · {% endif %}"
        "{% if left %}🔴 {{ left | join(', ') }} left{% endif %}. "
        "Online: {{ new | join(', ') if new else 'nobody' }} "
        "({{ new | count }}/{{ trigger.to_state.attributes.max | default('?') }}).")],
    "mode": "queued",
    "max": 5,
  },
  "bigbox_monitor_stale": {
    "alias": "bigbox: monitor agent silent",
    "description": "No successful monitor push for 6+ minutes.",
    "trigger": [{"platform": "time_pattern", "minutes": "/5"}],
    "condition": [{"condition": "template", "value_template":
        "{{ states('binary_sensor.bigbox_monitor') != 'on' or "
        "(now() - state_attr('binary_sensor.bigbox_monitor','last_run')|as_datetime|default(now()-timedelta(days=1))).total_seconds() > 360 }}"}],
    "action": [notify("⚠️ bigbox monitor silent",
        "No health data from bigbox since "
        "{{ state_attr('binary_sensor.bigbox_monitor','last_run') }}.")],
    "mode": "single",
  },
}

if __name__ == "__main__":
    # remove the earlier test automation if present
    try:
        api("DELETE", "/api/config/automation/config/bigbox_test_xyz")
    except Exception:
        pass
    for aid, cfg in AUTOMATIONS.items():
        st, body = api("POST", f"/api/config/automation/config/{aid}", cfg)
        print(f"{aid:28} {st} {body[:80]}")
    # reload so they take effect immediately
    print(api("POST", "/api/services/automation/reload", {}))
