#!/usr/bin/env python3
"""
Add the software-updates input to agent policies that predate it.

vms/updates.py writes pending package and container-image updates to
/var/log/elastic/updates.ndjson on every host; the software-updates Custom
Logs input ships that file as dataset software_updates. setup-policies.py
includes it in every policy it creates — but it only ever *creates*, so a
stack whose policies already exist never gets it from there, and neither does
a restart. This adds it to each existing agent policy that lacks it.

    ./add-update-inputs.py --check   # report which policies lack it, change nothing
    ./add-update-inputs.py           # add it

The collector itself has to be on each host too: re-running that host's agent
installer does it, or, without re-enrolling,

    sudo ./vms/install-host-tools.sh

Fleet Server's own policy is skipped — the stack VM's agent is the one that
reports for that machine.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_generator():
    """Import setup-policies.py for its API helper and the input definition.

    The hyphen in the filename means this cannot be a plain import. Importing it
    reads ../stack/.env and makes no API calls until its main() runs.
    """
    path = os.path.join(HERE, "setup-policies.py")
    spec = importlib.util.spec_from_file_location("setup_policies", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GEN = load_generator()


def has_updates_input(pkg_policies, policy_id):
    """True if any Custom Logs integration on this agent policy ships the dataset.

    Matched on the dataset rather than the name, so an input someone added by
    hand in the Fleet UI under another name counts too.
    """
    for p in pkg_policies:
        ids = p.get("policy_ids") or [p.get("policy_id")]
        if policy_id not in ids or p["package"]["name"] != "log":
            continue
        for inp in p.get("inputs", []):
            for st in inp.get("streams", []):
                ds = ((st.get("vars") or {}).get("data_stream.dataset") or {}).get("value")
                if ds == GEN.UPDATES_DATASET:
                    return True
    return False


def main():
    check_only = bool(sys.argv[1:]) and sys.argv[1] in ("--check", "-n", "--dry-run")

    policies = GEN.api("/api/fleet/agent_policies?perPage=200")["items"]
    pkg_policies = GEN.api("/api/fleet/package_policies?perPage=1000")["items"]

    todo = []
    print("\nAgent policies:")
    for pol in sorted(policies, key=lambda p: p["id"]):
        if pol.get("has_fleet_server") or pol.get("is_managed"):
            print("   %-20s skipped (Fleet Server / managed)" % pol["id"])
            continue
        if has_updates_input(pkg_policies, pol["id"]):
            print("   %-20s ok" % pol["id"])
            continue
        print("   %-20s NEEDS the %s input" % (pol["id"], GEN.UPDATES_NAME))
        todo.append(pol["id"])

    if not todo:
        print("\nNothing to do.")
        return 0
    if check_only:
        print("\n--check given: nothing changed. Re-run without it to apply.")
        return 0

    print("\nAdding:")
    for pid in todo:
        GEN.add(GEN.UPDATES_NAME, pid, "log", GEN.software_updates())

    print("\nDone. Agents pick this up on their next check-in. Each host also"
          "\nneeds the collector — sudo ./vms/install-host-tools.sh there — or"
          "\nthe input tails a file that never appears and reports nothing.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print("!! %s" % exc, file=sys.stderr)
        sys.exit(1)
