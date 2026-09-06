#!/usr/bin/env python3
"""
bigbox-monitor — collect bigbox health + Minecraft state, push to Home Assistant.

Runs as a systemd --user timer every ~60s. Reads config from monitor.env
(HA_URL, HA_TOKEN, NOTIFY_TARGET, MC_CONTAINER). Everything is best-effort:
a failing probe is logged and skipped, the rest still publish.

Entities published (all prefixed sensor.bigbox_ / binary_sensor.bigbox_):
  load, memory, cpu_temp, gpu, disk_max, disk_<mount>, zpool_<pool>,
  smart, smart_<dev>, containers, backup, backup_age_hours,
  mc_players, mc_online (binary), mc_<player> (binary, per rostered player),
  monitor (binary heartbeat)
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROSTER_FILE = HERE / "mc_roster.json"     # every player ever seen
ONLINE_FILE = HERE / "mc_online.json"     # who was online on the previous run

# ── config ──────────────────────────────────────────────────────────
def load_env():
    env = {}
    envfile = HERE / "monitor.env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: os.environ[k] for k in
                ("HA_URL", "HA_TOKEN", "MC_CONTAINER", "NOTIFY_TARGET")
                if k in os.environ})
    return env

CFG = load_env()
HA_URL = CFG.get("HA_URL", "http://10.0.0.90:8123").rstrip("/")
HA_TOKEN = CFG.get("HA_TOKEN", "")
MC_CONTAINER = CFG.get("MC_CONTAINER", "spencer-smp")

def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)

def run(cmd, timeout=15):
    """Run a command, return stdout (str) or '' on any failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError) as e:
        log("cmd failed:", " ".join(map(str, cmd)), "-", e)
        return ""

# ── HA REST ─────────────────────────────────────────────────────────
_ha_dead = False

def ha_request(method, path, payload=None, timeout=8):
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {HA_TOKEN}",
                 "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()

def push(entity_id, state, attrs=None):
    global _ha_dead
    if _ha_dead:
        return
    attrs = dict(attrs or {})
    attrs.setdefault("source", "bigbox-monitor")
    try:
        ha_request("POST", f"/api/states/{entity_id}",
                   {"state": str(state)[:255], "attributes": attrs})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log("HA unreachable, skipping the rest of this run:", e)
        _ha_dead = True

def logbook_log(name, message, entity_id):
    """Write a one-line entry into HA's Logbook (logbook.log service)."""
    if _ha_dead:
        return
    try:
        ha_request("POST", "/api/services/logbook/log",
                   {"name": name, "message": message,
                    "entity_id": entity_id, "domain": "minecraft"})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log("logbook.log failed:", e)

# ── collectors ──────────────────────────────────────────────────────
def collect_load():
    try:
        la = os.getloadavg()
        n = os.cpu_count() or 1
        push("sensor.bigbox_load", round(la[0], 2), {
            "friendly_name": "bigbox load (1m)", "icon": "mdi:chip",
            "load_5m": round(la[1], 2), "load_15m": round(la[2], 2),
            "cpu_count": n, "unit_of_measurement": "",
        })
        push("sensor.bigbox_cpu", min(100, round(100 * la[0] / n, 1)), {
            "friendly_name": "bigbox CPU", "icon": "mdi:chip",
            "unit_of_measurement": "%", "state_class": "measurement",
        })
    except OSError as e:
        log("load:", e)

def collect_memory():
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":")
            info[k] = int(v.strip().split()[0])  # kB
        total, avail = info["MemTotal"], info["MemAvailable"]
        used_pct = round(100 * (total - avail) / total, 1)
        push("sensor.bigbox_memory", used_pct, {
            "friendly_name": "bigbox memory used", "icon": "mdi:memory",
            "unit_of_measurement": "%", "state_class": "measurement",
            "total_gb": round(total / 2**20, 1),
            "used_gb": round((total - avail) / 2**20, 1),
            "available_gb": round(avail / 2**20, 1),
            "swap_used_gb": round((info.get("SwapTotal", 0) - info.get("SwapFree", 0)) / 2**20, 1),
        })
    except (OSError, KeyError, ValueError) as e:
        log("memory:", e)

def collect_cpu_temp():
    best = None
    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            name = (hwmon / "name").read_text().strip()
        except OSError:
            continue
        if name not in ("coretemp", "k10temp", "zenpower", "acpitz"):
            continue
        for t in hwmon.glob("temp*_input"):
            try:
                val = int(t.read_text().strip()) / 1000
                label_f = t.with_name(t.name.replace("_input", "_label"))
                label = label_f.read_text().strip() if label_f.exists() else name
                if "Package" in label or best is None:
                    best = val
            except (OSError, ValueError):
                pass
    if best is not None:
        push("sensor.bigbox_cpu_temp", round(best, 1), {
            "friendly_name": "bigbox CPU temp", "icon": "mdi:thermometer",
            "unit_of_measurement": "°C", "device_class": "temperature",
        })

def collect_gpu():
    out = run(["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu,"
               "memory.used,memory.total,power.draw", "--format=csv,noheader,nounits"])
    if not out.strip():
        return
    f = [x.strip() for x in out.strip().splitlines()[0].split(",")]
    try:
        push("sensor.bigbox_gpu", int(float(f[2])), {
            "friendly_name": "bigbox GPU util", "icon": "mdi:expansion-card",
            "unit_of_measurement": "%", "state_class": "measurement", "name": f[0],
            "temp_c": int(float(f[1])), "mem_used_mb": int(float(f[3])),
            "mem_total_mb": int(float(f[4])), "power_w": round(float(f[5]), 1),
        })
    except (ValueError, IndexError) as e:
        log("gpu parse:", e, f)

def collect_disks():
    out = run(["df", "-B1", "--output=source,fstype,size,used,avail,pcent,target"])
    if not out.strip():
        return
    rows, worst, worst_name = {}, -1, ""
    for line in out.strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        src, fstype, size, used, avail, pcent, target = parts[:6] + [" ".join(parts[6:])]
        if fstype in ("tmpfs", "devtmpfs", "efivarfs", "overlay", "squashfs"):
            continue
        if target.startswith(("/boot", "/sys", "/run", "/dev")):
            continue
        pct = int(pcent.rstrip("%"))
        slug = re.sub(r"[^a-z0-9]+", "_", target.lower()).strip("_") or "root"
        rows[target] = {"pct": pct, "size_gb": round(int(size) / 2**30, 1),
                        "used_gb": round(int(used) / 2**30, 1),
                        "free_gb": round(int(avail) / 2**30, 1), "device": src}
        push(f"sensor.bigbox_disk_{slug}", pct, {
            "friendly_name": f"bigbox disk {target}", "icon": "mdi:harddisk",
            "unit_of_measurement": "%", "state_class": "measurement", "mount": target, "device": src,
            "size_gb": rows[target]["size_gb"], "free_gb": rows[target]["free_gb"],
        })
        if pct > worst:
            worst, worst_name = pct, target
    push("sensor.bigbox_disk_max", worst, {
        "friendly_name": "bigbox fullest disk", "icon": "mdi:harddisk",
        "unit_of_measurement": "%", "state_class": "measurement", "mount": worst_name,
        "filesystems": rows,
    })

def collect_zfs():
    lst = run(["zpool", "list", "-H", "-o", "name,health,capacity,fragmentation,free,size"])
    xout = run(["zpool", "status", "-x"])
    for line in lst.strip().splitlines():
        name, health, cap, frag, free, size = line.split("\t")
        push(f"sensor.bigbox_zpool_{name}", health, {
            "friendly_name": f"bigbox zpool {name}", "icon": "mdi:database",
            "capacity_pct": int(cap.rstrip("%")),
            "fragmentation_pct": int(frag.rstrip("%")),
            "free": free, "size": size,
            "healthy": health == "ONLINE" and "all pools are healthy" in xout,
        })

SMART_DEVS = ["sda", "sdb", "sdc", "sdd", "sde", "sdf", "sdg"]
def collect_smart():
    results = {}
    for dev in SMART_DEVS:
        path = f"/dev/{dev}"
        if not Path(path).exists():
            continue
        out = run(["sudo", "-n", "smartctl", "-H", "-i", path], timeout=20)
        if not out:
            results[dev] = "UNKNOWN"
            continue
        m = re.search(r"(?:SMART overall-health self-assessment test result|"
                      r"SMART Health Status):\s*(\S+)", out)
        status = m.group(1).upper() if m else "UNKNOWN"
        model = re.search(r"(?:Device Model|Model Number):\s*(.+)", out)
        results[dev] = status
        push(f"sensor.bigbox_smart_{dev}", status, {
            "friendly_name": f"bigbox SMART {dev}", "icon": "mdi:harddisk",
            "model": model.group(1).strip() if model else "",
        })
    vals = set(results.values())
    if "FAILED" in vals:
        overall = "FAILED"
    elif vals and vals <= {"PASSED", "OK"}:
        overall = "OK"
    else:
        overall = "UNKNOWN"          # sudo not set up yet, or a drive not reporting
    push("sensor.bigbox_smart", overall, {
        "friendly_name": "bigbox SMART overall", "icon": "mdi:harddisk-plus",
        "drives": results,
    })

def collect_containers():
    out = run(["podman", "ps", "-a", "--format",
               "{{.Names}}\t{{.State}}\t{{.Status}}"])
    if not out.strip():
        return
    states, down = {}, []
    for line in out.strip().splitlines():
        name, state, status = line.split("\t", 2)
        states[name] = status
        healthy = state == "running" and "unhealthy" not in status.lower()
        if not healthy:
            down.append(name)
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        push(f"sensor.bigbox_container_{slug}",
             "unhealthy" if (state == "running" and "unhealthy" in status.lower())
             else state,
             {"friendly_name": name, "icon": "mdi:cube-outline",
              "status": status})
    push("sensor.bigbox_containers", len(states) - len(down), {
        "friendly_name": "bigbox containers up", "icon": "mdi:server",
        "unit_of_measurement": f"/{len(states)}",
        "total": len(states), "down": down, "status": states,
    })

def collect_backup():
    """Read the nightly backup's status file and publish freshness + result."""
    f = Path("/media/backup/.backup-status.json")
    if not f.exists():
        push("sensor.bigbox_backup", "unknown", {
            "friendly_name": "bigbox backup", "icon": "mdi:backup-restore",
            "note": "no /media/backup/.backup-status.json yet"})
        return
    try:
        st = json.loads(f.read_text())
    except (OSError, ValueError) as e:
        log("backup status unreadable:", e)
        push("sensor.bigbox_backup", "unknown",
             {"friendly_name": "bigbox backup", "icon": "mdi:backup-restore"})
        return
    age_h = round((time.time() - st.get("ts", 0)) / 3600, 1)
    if not st.get("ok"):
        state = "failed"
    elif age_h > 30:
        state = "stale"
    else:
        state = "ok"
    push("sensor.bigbox_backup", state, {
        "friendly_name": "bigbox backup", "icon": "mdi:backup-restore",
        "last_run": st.get("iso"), "age_hours": age_h,
        "duration_s": st.get("duration_s"), "steps": st.get("steps", {}),
        "dest_free_gb": round(st.get("dest_free_bytes", 0) / 2**30, 1),
    })
    push("sensor.bigbox_backup_age_hours", age_h, {
        "friendly_name": "bigbox backup age", "icon": "mdi:backup-restore",
        "unit_of_measurement": "h", "state_class": "measurement"})

MC_LIST_RE = re.compile(r"There are (\d+) of a max of (\d+) players online:\s*(.*)")
def collect_minecraft():
    out = run(["podman", "exec", MC_CONTAINER, "rcon-cli", "list"], timeout=20)
    m = MC_LIST_RE.search(out)
    if not m:
        push("binary_sensor.bigbox_mc_online", "off",
             {"friendly_name": "SMP server", "device_class": "connectivity"})
        return
    count, maximum, names = int(m.group(1)), int(m.group(2)), m.group(3).strip()
    players = sorted(p.strip() for p in names.split(",") if p.strip())
    push("binary_sensor.bigbox_mc_online", "on",
         {"friendly_name": "SMP server", "device_class": "connectivity"})
    push("sensor.bigbox_mc_players", count, {
        "friendly_name": "SMP players online", "icon": "mdi:minecraft",
        "unit_of_measurement": "players", "state_class": "measurement",
        "max": maximum, "players": players,
    })
    # roster: every player ever seen gets a binary_sensor for HA history
    try:
        roster = set(json.loads(ROSTER_FILE.read_text())) if ROSTER_FILE.exists() else set()
    except (OSError, ValueError):
        roster = set()
    roster |= set(players)
    try:
        ROSTER_FILE.write_text(json.dumps(sorted(roster)))
    except OSError:
        pass
    for p in roster:
        slug = re.sub(r"[^a-z0-9]+", "_", p.lower()).strip("_")
        push(f"binary_sensor.bigbox_mc_{slug}",
             "on" if p in players else "off",
             {"friendly_name": f"SMP: {p}", "icon": "mdi:account",
              "player": p, "device_class": "presence"})

    # join / leave: diff this run's online set against the last one and
    # write a Logbook line per change. Only runs when rcon succeeded, so a
    # transient failure flaps binary_sensor.bigbox_mc_online but never
    # fabricates a join/leave storm.
    try:
        was_online = set(json.loads(ONLINE_FILE.read_text())) if ONLINE_FILE.exists() else None
    except (OSError, ValueError):
        was_online = None
    now_online = set(players)
    if was_online is not None:        # skip first run — would log everyone as joining
        for p in sorted(now_online - was_online):
            logbook_log(p, "joined the SMP", "sensor.bigbox_mc_players")
        for p in sorted(was_online - now_online):
            logbook_log(p, "left the SMP", "sensor.bigbox_mc_players")
    try:
        ONLINE_FILE.write_text(json.dumps(sorted(now_online)))
    except OSError:
        pass

def main():
    if not HA_TOKEN:
        log("no HA_TOKEN in monitor.env — aborting")
        sys.exit(1)
    t0 = time.time()
    for fn in (collect_load, collect_memory, collect_cpu_temp, collect_gpu,
               collect_disks, collect_zfs, collect_smart, collect_containers,
               collect_backup, collect_minecraft):
        try:
            fn()
        except Exception as e:                       # never let one probe kill the run
            log(fn.__name__, "unexpected:", e)
    push("binary_sensor.bigbox_monitor", "on", {
        "friendly_name": "bigbox monitor agent",
        "device_class": "connectivity",
        "host": socket.gethostname(),
        "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": round(time.time() - t0, 1),
    })
    # fixed 0 / 100 reference lines so the disk graph keeps a full 0–100% axis
    push("sensor.bigbox_ref_0", 0, {"friendly_name": "0%",
         "unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:blank"})
    push("sensor.bigbox_ref_100", 100, {"friendly_name": "100%",
         "unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:blank"})
    log(f"done in {time.time() - t0:.1f}s")

if __name__ == "__main__":
    main()
