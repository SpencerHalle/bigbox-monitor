#!/usr/bin/env bash
# bigbox-monitor-setup.sh — the ONLY part of the monitor that needs root.
#
#   sudo bash bigbox-monitor-setup.sh
#
# Does two things:
#   1. Lets your user run `smartctl -H`/`-i` on the disks without a password
#      (read-only health checks — nothing else).
#   2. Enables systemd "linger" for your user so the monitor timer keeps
#      running when you're not logged in.
#
# The monitor agent itself runs as your normal user, no root. Idempotent.
set -euo pipefail

TARGET_USER="${SUDO_USER:-spencer}"
SMARTCTL="$(command -v smartctl || echo /usr/sbin/smartctl)"
SUDOERS=/etc/sudoers.d/bigbox-monitor

echo "==> user: $TARGET_USER   smartctl: $SMARTCTL"

# ── 1. scoped sudoers for read-only SMART health ────────────────────
tmp="$(mktemp)"
{
  echo "# bigbox-monitor: read-only drive health only. Installed $(date +%F)."
  echo "Cmnd_Alias BIGBOX_SMART = \\"
  echo "    $SMARTCTL -H /dev/sd[a-z], \\"
  echo "    $SMARTCTL -H -i /dev/sd[a-z], \\"
  echo "    $SMARTCTL -i -H /dev/sd[a-z], \\"
  echo "    $SMARTCTL -H -A /dev/sd[a-z]"
  echo "$TARGET_USER ALL=(root) NOPASSWD: BIGBOX_SMART"
} > "$tmp"
chmod 440 "$tmp"

if visudo -c -f "$tmp" >/dev/null 2>&1; then
  install -m 440 -o root -g root "$tmp" "$SUDOERS"
  rm -f "$tmp"
  echo "==> installed $SUDOERS"
else
  echo "!! sudoers validation FAILED — not installing:"; cat "$tmp"; rm -f "$tmp"; exit 1
fi

# ── 2. linger so the --user timer survives logout / reboot ──────────
loginctl enable-linger "$TARGET_USER"
echo "==> linger enabled for $TARGET_USER"

echo
echo "✅ Done. Verify:"
echo "   sudo -l -U $TARGET_USER | grep -i smart"
echo "   sudo -n $SMARTCTL -H /dev/sda | tail -1"
echo
echo "To undo later:  sudo rm $SUDOERS && sudo loginctl disable-linger $TARGET_USER"
