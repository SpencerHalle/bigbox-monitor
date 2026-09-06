# bigbox-monitor

Pushes bigbox health + Minecraft state to Home Assistant every 60s. No MQTT,
no root (except a scoped `smartctl -H` sudoers line). Lives in `~/homelab/monitor/`.

## Pieces

| File | What |
|---|---|
| `monitor.py` | the agent — collectors + HA REST push |
| `monitor.env` | `HA_URL`, `HA_TOKEN`, `MC_CONTAINER` (chmod 600) |
| `mc_roster.json` | every MC player ever seen (drives the per-player sensors) |
| `mc_online.json` | who was on last tick — diffed each run to write join/leave Logbook lines |
| `bigbox-monitor.service` / `.timer` | systemd **user** units (`~/.config/systemd/user/`) |
| `bigbox-monitor-setup.sh` | the one root step: smartctl sudoers + `enable-linger` |
| `ha_automations.py` | (re)creates the HA automations via the config API |
| `ha_dashboard.py` + `dashboard-bigbox.yaml` / `dashboard-minecraft.yaml` | (re)create the two dashboards via the HA websocket API|

The nightly **backup** job (`~/homelab/backup/`) is separate but wired in here:
`monitor.py`'s `collect_backup()` reads `/media/backup/.backup-status.json` and
publishes `sensor.bigbox_backup` + `sensor.bigbox_backup_age_hours`, and
`ha_automations.py` carries the alert for it.

## Entities published

`sensor.bigbox_` — `cpu`, `load`, `memory`, `cpu_temp`, `gpu`, `disk_max`,
`disk_<mount>` (×4), `zpool_tank`, `smart` + `smart_sd[a-g]`, `containers`,
`container_<name>`, `backup` (ok/failed/stale/unknown), `backup_age_hours`,
`mc_players`.
`binary_sensor.bigbox_` — `monitor` (heartbeat, has `last_run`), `mc_online`,
`mc_<player>` (one per rostered player, on = currently online).

## Operate

```bash
systemctl --user status bigbox-monitor.timer
journalctl --user -u bigbox-monitor -n 40 --no-pager
python3 ~/homelab/monitor/monitor.py            # run once, see stderr

# after editing monitor.py: just save it, next tick picks it up
# after editing automations/dashboard:
echo "$HA_TOKEN" | python3 ha_automations.py
echo "$HA_TOKEN" | python3 ha_dashboard.py dashboard-bigbox.yaml bigbox-panel Bigbox mdi:server
 echo "$HA_TOKEN" | python3 ha_dashboard.py dashboard-minecraft.yaml minecraft-panel Minecraft mdi:minecraft
```

HA host is `10.0.0.90:8123` on the LAN (bigbox can't reach the tailscale IP).
Alerts go to `notify.mobile_app_pixel_9_pro` — change `NOTIFY` in
`ha_automations.py` and re-run to retarget.

## Automations

disk >90% (10-min debounce) · SMART FAILED (critical) · ZFS pool not ONLINE
(critical) · container down 5+ min · nightly backup failed/stale 10+ min ·
SMP player joined/left (every change, names who) · monitor agent silent 6+ min.

The join/leave detection is poll-based (~60s): `monitor.py` diffs the online
list against `mc_online.json` and writes a `logbook.log` line per change
(shown in the dashboard's **Join / leave log** card, filtered on
`sensor.bigbox_mc_players`). A session shorter than one poll interval can be
missed entirely.
