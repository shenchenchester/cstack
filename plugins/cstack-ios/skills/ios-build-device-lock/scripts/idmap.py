#!/usr/bin/env python3
"""Map any phone identifier (hardware UDID, CoreDevice UUID, device name) to the hardware UDID.

usage: idmap.py <id>   -> prints the hardware UDID, or nothing if unknown.
The devicectl device list is cached in DEVLOCK_HOME/idmap.json (~/.devlock) and refreshed on a miss.
"""
import json, os, re, subprocess, sys, tempfile

CACHE = os.path.join(os.environ.get("DEVLOCK_HOME", os.path.expanduser("~/.devlock")), "idmap.json")
UDID = re.compile(r"^([0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9a-f]{40})$")


def refresh():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as t:
        path = t.name
    try:
        subprocess.run(["/usr/bin/xcrun", "devicectl", "list", "devices", "--json-output", path],
                       capture_output=True, timeout=30)
        devs = json.load(open(path)).get("result", {}).get("devices", [])
    except Exception:
        devs = []
    finally:
        os.unlink(path)
    table = {}
    for d in devs:
        u = d.get("hardwareProperties", {}).get("udid")
        if not u:
            continue
        for k in (u, d.get("identifier"), d.get("deviceProperties", {}).get("name")):
            if k:
                table[k] = u
    with open(CACHE, "w") as f:
        json.dump(table, f)
    return table


def main():
    key = sys.argv[1]
    if UDID.match(key):
        print(key)
        return
    try:
        table = json.load(open(CACHE))
    except Exception:
        table = {}
    if key not in table:
        table = refresh()
    if key in table:
        print(table[key])


if __name__ == "__main__":
    main()
