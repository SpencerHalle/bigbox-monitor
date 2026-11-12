#!/usr/bin/env python3
"""
pi_health.py — one-shot health probe for testserver (the Raspberry Pi 5 that runs Robbie).

Not installed on the Pi: monitor.py on bigbox pipes this file over ssh each run
(`ssh spencer@10.0.0.17 python3 - < pi_health.py`) and reads one JSON object back.
Unprivileged only (dmesg_restrict=0 on the Pi, vcgencmd via the video group), stdlib only.
"""
import json
import os
import re
import shutil
import subprocess
import time

ROOT_DEV = "mmcblk0p2"


def sh(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


out = {"ts": int(time.time())}

# uptime / load / memory
out["uptime_s"] = int(float(read("/proc/uptime", "0").split()[0]))
out["load"] = [round(x, 2) for x in os.getloadavg()]
mem = {}
for line in read("/proc/meminfo").splitlines():
    k, v = line.split(":", 1)
    mem[k] = int(v.split()[0])
if mem:
    out["mem_used_pct"] = round(100 * (mem["MemTotal"] - mem["MemAvailable"]) / mem["MemTotal"], 1)
    out["mem_avail_mb"] = mem["MemAvailable"] // 1024
    out["swap_used_mb"] = (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) // 1024

# temperature + throttling
t = read("/sys/class/thermal/thermal_zone0/temp")
out["temp_c"] = round(int(t) / 1000, 1) if t.isdigit() else None
m = re.search(r"throttled=(0x[0-9a-fA-F]+)", sh(["vcgencmd", "get_throttled"]))
out["throttled"] = m.group(1).lower() if m else None
m = re.search(r"=([\d.]+)V", sh(["vcgencmd", "pmic_read_adc", "EXT5V_V"]))
out["ext5v_v"] = round(float(m.group(1)), 2) if m else None

# root filesystem: free space, read-only, ext4 error counter, lifetime writes
du = shutil.disk_usage("/")
out["root_free_gb"] = round(du.free / 2**30, 1)
out["root_used_pct"] = round(100 * du.used / du.total, 1)
opts = ""
for line in read("/proc/mounts").splitlines():
    f = line.split()
    if len(f) >= 4 and f[1] == "/":
        opts = f[3]
out["root_ro"] = opts.split(",")[0] == "ro"
out["root_opts"] = opts
sysfs = f"/sys/fs/ext4/{ROOT_DEV}"
ec = read(f"{sysfs}/errors_count")
out["ext4_errors"] = int(ec) if ec.isdigit() else None
lw = read(f"{sysfs}/lifetime_write_kbytes")
out["lifetime_write_gb"] = round(int(lw) / 2**20, 1) if lw.isdigit() else None
# sectors written since boot -> average MB/day this boot
for line in read("/proc/diskstats").splitlines():
    f = line.split()
    if len(f) > 9 and f[2] == "mmcblk0":
        written_mb = int(f[9]) * 512 / 2**20
        out["written_since_boot_mb"] = round(written_mb)
        if out["uptime_s"] > 3600:
            out["write_mb_per_day"] = round(written_mb / (out["uptime_s"] / 86400))

# kernel log since boot: storage errors and undervoltage events
dmesg = sh(["dmesg"])
err_re = re.compile(
    r"(mmc\d+: .*(error|timeout|timed out|CRC|reset)|EXT4-fs (error|warning)|"
    r"I/O error|Buffer I/O|blk_update_request|remount.*read-only|Remounting filesystem read-only)",
    re.I)
errs = [l for l in dmesg.splitlines() if err_re.search(l)]
out["kernel_disk_errors"] = len(errs)
out["kernel_disk_errors_last"] = errs[-1][-200:] if errs else ""
out["undervoltage_events"] = len(re.findall(r"Undervoltage detected", dmesg))

# SD card identity
dev = "/sys/block/mmcblk0/device"
out["sd"] = {k: read(f"{dev}/{k}") for k in ("name", "manfid", "oemid", "date")}

# Robbie (openclaw gateway, systemd --user)
out["robbie"] = sh(["systemctl", "--user", "is-active", "openclaw-gateway.service"]).strip() or "inactive"

print(json.dumps(out))
