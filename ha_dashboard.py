#!/usr/bin/env python3
"""Create/replace a Lovelace dashboard via the HA websocket API.
Usage: echo "$TOKEN" | python3 ha_dashboard.py <file.yaml> <url-path-with-hyphen> <Title> <icon> [host]
No websocket library — minimal RFC6455 client (text frames, client-masked)."""
import json, os, secrets, socket, struct, sys, base64, hashlib

YAML_PATH = sys.argv[1]
URL_PATH = sys.argv[2] if len(sys.argv) > 2 else "bigbox-panel"
TITLE = sys.argv[3] if len(sys.argv) > 3 else "Bigbox"
ICON = sys.argv[4] if len(sys.argv) > 4 else "mdi:server"
HOST = sys.argv[5] if len(sys.argv) > 5 else "10.0.0.90"
PORT = 8123
TOKEN = sys.stdin.read().strip()

# tiny YAML: reuse HA's own parser would be ideal; instead require pyyaml OR
# accept a pre-dumped JSON. We shell out to python3 -c with pyyaml if present.
try:
    import yaml
    CONFIG = yaml.safe_load(open(YAML_PATH))
except ImportError:
    print("need pyyaml locally, or pass a .json file", file=sys.stderr)
    if YAML_PATH.endswith(".json"):
        CONFIG = json.load(open(YAML_PATH))
    else:
        sys.exit(2)

sock = socket.create_connection((HOST, PORT), timeout=10)
key = base64.b64encode(secrets.token_bytes(16)).decode()
sock.sendall(
    f"GET /api/websocket HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
    f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
)
buf = b""
while b"\r\n\r\n" not in buf:
    buf += sock.recv(4096)
head, _, rest = buf.partition(b"\r\n\r\n")
assert b"101" in head.split(b"\r\n")[0], head.split(b"\r\n")[0]
_leftover = bytearray(rest)          # bytes of the first WS frame already read

def send(obj):
    data = json.dumps(obj).encode()
    hdr = bytearray([0x81])
    n = len(data)
    mask = secrets.token_bytes(4)
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
    else:
        hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
    hdr += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(hdr) + masked)

def recv():
    def rd(k):
        while len(_leftover) < k:
            _leftover.extend(sock.recv(4096))
        b = bytes(_leftover[:k])
        del _leftover[:k]
        return b
    b0, b1 = rd(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", rd(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", rd(8))[0]
    return json.loads(rd(n))

assert recv()["type"] == "auth_required"
send({"type": "auth", "access_token": TOKEN})
assert recv()["type"] == "auth_ok", "auth failed"

_id = 0
def cmd(**kw):
    global _id
    _id += 1
    send({"id": _id, **kw})
    while True:
        m = recv()
        if m.get("id") == _id and m.get("type") == "result":
            return m

dbs = cmd(type="lovelace/dashboards/list")["result"]
existing = next((d for d in dbs if d.get("url_path") == URL_PATH), None)
if not existing:
    r = cmd(type="lovelace/dashboards/create", url_path=URL_PATH,
            title=TITLE, icon=ICON, mode="storage", show_in_sidebar=True)
    print(f"create {URL_PATH}:", r.get("success"), r.get("error", ""))
else:
    print(f"{URL_PATH} exists — updating config")

r = cmd(type="lovelace/config/save", url_path=URL_PATH, config=CONFIG)
print("save config:", r.get("success"), r.get("error", ""))
sock.close()
