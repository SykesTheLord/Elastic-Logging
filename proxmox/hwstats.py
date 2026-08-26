#!/usr/bin/env python3
"""
Emit hardware telemetry as NDJSON for Elastic Agent to tail.

Elastic Agent has no native lm-sensors or SMART input, so this fills the gap:
one JSON document per reading, appended to a file that the Custom Logs
integration picks up with its ndjson parser. Everything else the Proxmox host
needs (CPU, memory, filesystem, network, systemd units, journal) is covered by
the stock System and Journald integrations.

Depends only on the Python 3 stdlib plus the `sensors`, `smartctl` and `zpool`
binaries. Any of those being absent degrades gracefully to fewer documents.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

OUT = os.environ.get("HWSTATS_OUT", "/var/log/elastic/hwstats.ndjson")
HOST = os.uname().nodename


def run(cmd, timeout=30):
    """Run a command, returning stdout or None. Never raises."""
    if not shutil.which(cmd[0]):
        return None
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        # smartctl uses its exit status as a bitfield: bits 0-1 mean the command
        # failed, higher bits are disk warnings that still come with valid JSON.
        if p.returncode and not p.stdout.strip():
            return None
        return p.stdout
    except (subprocess.TimeoutExpired, OSError):
        return None


def doc(kind, payload):
    return {
        "@timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "host": {"name": HOST},
        "event": {"module": "proxmox", "dataset": "proxmox_hw", "kind": "metric"},
        "hw": dict({"kind": kind}, **payload),
    }


def temperatures():
    """lm-sensors readings, one document per labelled input."""
    out = run(["sensors", "-j"])
    if not out:
        return
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return
    for chip, features in data.items():
        if not isinstance(features, dict):
            continue
        for label, inputs in features.items():
            if not isinstance(inputs, dict):
                continue
            for key, value in inputs.items():
                if not key.endswith("_input") or not isinstance(value, (int, float)):
                    continue
                # Chips expose fans and voltages through the same shape; only
                # tempN_input is a temperature.
                if not key.startswith("temp"):
                    continue
                yield doc("temperature", {"sensor": {
                    "chip": chip, "label": label, "celsius": round(float(value), 1),
                }})


def block_devices():
    out = run(["lsblk", "-dn", "-o", "NAME,TYPE"])
    if not out:
        return []
    devs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "disk":
            devs.append("/dev/" + parts[0])
    return devs


def _sata_attrs(data):
    """Pull the SMART attributes that actually predict failure."""
    wanted = {
        5: "reallocated_sectors",
        9: "power_on_hours",
        187: "reported_uncorrect",
        197: "pending_sectors",
        198: "offline_uncorrectable",
    }
    found = {}
    for attr in (data.get("ata_smart_attributes") or {}).get("table", []):
        name = wanted.get(attr.get("id"))
        if name:
            raw = (attr.get("raw") or {}).get("value")
            if isinstance(raw, int):
                found[name] = raw
    return found


def disks():
    """SMART health, temperature and wear for every physical disk."""
    for dev in block_devices():
        out = run(["smartctl", "--json", "-a", dev], timeout=60)
        if not out:
            continue
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            continue

        d = {
            "device": dev,
            "model": data.get("model_name"),
            "serial": data.get("serial_number"),
        }

        status = data.get("smart_status") or {}
        if "passed" in status:
            d["smart_passed"] = bool(status["passed"])

        temp = (data.get("temperature") or {}).get("current")
        if isinstance(temp, (int, float)):
            d["celsius"] = float(temp)

        poh = (data.get("power_on_time") or {}).get("hours")
        if isinstance(poh, int):
            d["power_on_hours"] = poh

        # NVMe reports wear and errors in its own health log.
        nvme = data.get("nvme_smart_health_information_log") or {}
        for src, dst in (("percentage_used", "percentage_used"),
                         ("media_errors", "media_errors"),
                         ("critical_warning", "critical_warning"),
                         ("available_spare", "available_spare")):
            if isinstance(nvme.get(src), int):
                d[dst] = nvme[src]

        d.update(_sata_attrs(data))
        yield doc("disk", {"disk": {k: v for k, v in d.items() if v is not None}})


def zpools():
    """ZFS pool health — the usual Proxmox root filesystem."""
    out = run(["zpool", "list", "-Hp", "-o", "name,size,alloc,free,capacity,health,fragmentation"])
    if not out:
        return
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) < 6:
            continue
        pool = {"name": f[0], "health": f[5]}
        for idx, key in ((1, "size_bytes"), (2, "allocated_bytes"), (3, "free_bytes"),
                         (4, "capacity_pct"), (6, "fragmentation_pct")):
            if idx < len(f):
                try:
                    pool[key] = int(f[idx].rstrip("%"))
                except ValueError:
                    pass
        status = run(["zpool", "status", "-x", f[0]]) or ""
        pool["healthy"] = "is healthy" in status or pool["health"] == "ONLINE"
        yield doc("zpool", {"zpool": pool})


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    lines = []
    for source in (temperatures, disks, zpools):
        try:
            lines.extend(json.dumps(d, separators=(",", ":")) for d in source())
        except Exception as exc:  # a broken sensor must not stop the rest
            lines.append(json.dumps(doc("collector_error", {
                "collector": source.__name__, "error": str(exc)}), separators=(",", ":")))
    if not lines:
        return 0
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
