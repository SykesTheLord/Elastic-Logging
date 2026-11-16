#!/usr/bin/env python3
"""
Report pending software updates as NDJSON for Elastic Agent to tail.

Three kinds of document per run, all dataset software_updates, told apart by
updates.kind:

  summary  one per run: package manager, packages pending, how many are
           security fixes, whether a reboot is needed, stale container images
  package  one per upgradable package: installed and candidate version, repo,
           whether it is a security update
  image    one per running container on a Docker host: whether the registry
           now serves a different image under the tag it was started from

Package managers: apt (Ubuntu, Debian, Proxmox VE) and pacman (Arch). The
package lists are refreshed first — apt-get update, or checkupdates, which
syncs into a private copy of the database and so never leaves the system half
upgraded the way a bare `pacman -Sy` would.

Container images are compared by digest against the registry with a HEAD
request, which Docker Hub does not count against its pull limit. An image
pinned to a digest, or built locally, has nothing to compare with and is
skipped. Note what this does and does not catch: a newer build pushed under
the same tag (linuxserver's :latest, a rebuilt :9.5.1) shows up; a newer tag
(9.5.2) does not — nothing tells a registry client that 9.5.2 supersedes 9.5.1.

Depends only on the Python 3 stdlib plus the tools it inspects. Anything
absent — no docker, no checkupdates — degrades to fewer documents, never an
error. Runs as root from elastic-updates.timer.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

OUT = os.environ.get("UPDATES_OUT", "/var/log/elastic/updates.ndjson")
HOST = os.uname().nodename
NOW = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run(cmd, timeout=600, ok=(0,)):
    """Run a command, returning (returncode, stdout). Never raises."""
    if not shutil.which(cmd[0]):
        return None, ""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, LC_ALL="C", LANG="C"))
        return p.returncode, p.stdout
    except (subprocess.TimeoutExpired, OSError):
        return None, ""


def doc(kind, updates, **extra):
    d = {
        "@timestamp": NOW,
        "host": {"name": HOST},
        "event": {"kind": "state", "module": "updates", "dataset": "software_updates"},
        "updates": dict({"kind": kind}, **updates),
    }
    d.update(extra)
    return d


# ── Package managers ────────────────────────────────────────────────────────

APT_LINE = re.compile(r"^(?P<name>[^/\s]+)/(?P<suites>\S+)\s+(?P<new>\S+)\s+(?P<arch>\S+)"
                      r"(?:\s+\[upgradable from: (?P<old>[^\]]+)\])?")


def apt():
    """Upgradable packages from apt, after refreshing the lists.

    A failed refresh is recorded rather than fatal: on a Proxmox host without
    a subscription the enterprise repository answers 401 on every update, and
    the other repositories' lists are still fresh.
    """
    rc, _ = run(["apt-get", "update", "-qq", "-o", "DPkg::Lock::Timeout=300"])
    refresh_ok = rc == 0
    _, out = run(["apt", "list", "--upgradable"])
    pkgs = []
    for line in out.splitlines():
        m = APT_LINE.match(line.strip())
        if not m:
            continue
        suites = m.group("suites").split(",")
        pkgs.append({
            "name": m.group("name"),
            "version": (m.group("old") or "").strip(),
            "candidate": m.group("new"),
            "repo": ",".join(suites),
            # Ubuntu's noble-security, Debian's bookworm-security and
            # trixie-security, and the bare stable-security alias all end so.
            "security": any(s.endswith("-security") for s in suites),
        })
    return pkgs, refresh_ok


def pacman():
    """Upgradable packages on Arch, via checkupdates (pacman-contrib)."""
    rc, out = run(["checkupdates"])
    # checkupdates exits 2 for "no updates", 1 for a failure.
    refresh_ok = rc in (0, 2)
    insecure = set()
    if shutil.which("arch-audit"):
        _, aud = run(["arch-audit", "-u", "-f", "%n"])
        insecure = {line.strip() for line in aud.splitlines() if line.strip()}
    pkgs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "->":
            pkgs.append({"name": parts[0], "version": parts[1], "candidate": parts[3],
                         "repo": "", "security": parts[0] in insecure})
    return pkgs, refresh_ok


def reboot_required():
    """Whether the machine is running something an installed update replaced.

    Ubuntu drops /var/run/reboot-required; Debian, Proxmox and Arch do not, so
    the kernel is checked directly as well: if the running kernel's modules
    are gone (Arch removes them on upgrade) or a newer kernel's are installed
    (Debian and Proxmox keep the old one), a reboot is pending.
    """
    if os.path.exists("/var/run/reboot-required"):
        return True
    running = os.uname().release
    mods = "/usr/lib/modules" if os.path.isdir("/usr/lib/modules") else "/lib/modules"
    if not os.path.isdir(mods):
        return False
    if not os.path.isdir(os.path.join(mods, running)):
        return True
    try:
        dirs = [d for d in os.listdir(mods)
                if os.path.isfile(os.path.join(mods, d, "modules.dep"))]
        newest = max(dirs, key=lambda d: os.path.getmtime(os.path.join(mods, d, "modules.dep")))
    except (OSError, ValueError):
        return False
    return newest != running


# ── Container images ────────────────────────────────────────────────────────

ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])
_tokens = {}


def parse_ref(ref):
    """Split an image reference into (registry, repository, tag), or None.

    Follows Docker's own rules: a first component with a dot, a colon or
    'localhost' is a registry; anything else is Docker Hub, where a bare name
    means library/<name>.
    """
    if "@" in ref:
        return None  # pinned by digest: by definition never stale
    name, tag = ref, "latest"
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = ref.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "docker.io", name
    if registry == "docker.io":
        registry = "registry-1.docker.io"
        if "/" not in repo:
            repo = "library/" + repo
    return registry, repo, tag


def _bearer(challenge):
    """Fetch an anonymous token for a `WWW-Authenticate: Bearer ...` challenge."""
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.pop("realm", None)
    if not realm:
        return None
    key = (realm, params.get("service"), params.get("scope"))
    if key not in _tokens:
        url = realm + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=20) as r:
            body = json.load(r)
        _tokens[key] = body.get("token") or body.get("access_token")
    return _tokens[key]


def remote_digest(registry, repo, tag):
    url = "https://%s/v2/%s/manifests/%s" % (registry, repo, tag)
    headers = {"Accept": ACCEPT}
    for _ in range(2):
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.headers.get("Docker-Content-Digest")
        except urllib.error.HTTPError as e:
            challenge = e.headers.get("WWW-Authenticate", "")
            if e.code == 401 and challenge.lower().startswith("bearer") and "Authorization" not in headers:
                token = _bearer(challenge)
                if token:
                    headers["Authorization"] = "Bearer " + token
                    continue
            raise
    return None


def images():
    """One document per running container's image."""
    if not os.path.exists("/var/run/docker.sock"):
        return []
    rc, out = run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.ID}}"], timeout=60)
    if rc != 0:
        return []
    docs, checked = [], {}
    for line in out.splitlines():
        try:
            cname, ref, cid = line.split("\t")
        except ValueError:
            continue
        _, raw = run(["docker", "inspect", "--format",
                      "{{json .Image}}", cid], timeout=30)
        image_id = (raw.strip().strip('"') or "")
        _, raw = run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}",
                      image_id], timeout=30)
        try:
            local = {d.split("@", 1)[1] for d in json.loads(raw or "null") or [] if "@" in d}
        except ValueError:
            local = set()

        parsed = parse_ref(ref)
        name, tag = ref, ""
        if parsed:
            name = ref.rsplit(":", 1)[0] if ":" in ref.rsplit("/", 1)[-1] else ref
            tag = parsed[2]
        upd = {"image_ref": ref}
        if not parsed:
            upd["status"] = "pinned"
        elif not local:
            # Built here or loaded from a tarball: no registry to compare with.
            upd["status"] = "local"
        else:
            try:
                if ref not in checked:
                    checked[ref] = remote_digest(*parsed)
                rd = checked[ref]
                upd["remote_digest"] = rd
                upd["local_digest"] = sorted(local)[0]
                if rd is None:
                    upd["status"] = "unknown"
                else:
                    upd["image_outdated"] = rd not in local
                    upd["status"] = "outdated" if upd["image_outdated"] else "current"
            except urllib.error.HTTPError as exc:
                # 404, or 401 even with an anonymous token: the registry has
                # no such public image. Built locally (Docker's containerd
                # image store gives those a digest too) or a private registry
                # that needs credentials this checker does not have.
                upd["status"] = "not_found" if exc.code in (401, 403, 404) else "error"
                upd["error"] = str(exc)[:200]
            except (urllib.error.URLError, OSError, ValueError) as exc:
                upd["status"] = "error"
                upd["error"] = str(exc)[:200]
        docs.append(doc("image", upd, container={
            "name": cname, "id": cid, "image": {"name": name, "tag": [tag] if tag else []}}))
    return docs


# ── Entry point ─────────────────────────────────────────────────────────────

def main():
    if shutil.which("apt-get"):
        manager, (pkgs, refreshed) = "apt", apt()
    elif shutil.which("pacman"):
        manager, (pkgs, refreshed) = "pacman", pacman()
    else:
        manager, pkgs, refreshed = "none", [], False

    docs = []
    for p in pkgs:
        docs.append(doc("package", {
            "candidate_version": p["candidate"], "repo": p["repo"],
            "security": p["security"], "manager": manager,
        }, package={"name": p["name"], "version": p["version"]},
            message="%s %s -> %s%s" % (p["name"], p["version"], p["candidate"],
                                       " (security)" if p["security"] else "")))
    imgs = images()
    docs.extend(imgs)

    pending = len(pkgs)
    security = sum(1 for p in pkgs if p["security"])
    outdated = sum(1 for d in imgs if d["updates"].get("image_outdated"))
    reboot = reboot_required()
    docs.append(doc("summary", {
        # security_pending, not security: package documents carry a boolean
        # updates.security in the same data stream, and whichever arrives
        # first fixes the mapping — the other would then be dropped as
        # malformed, silently.
        "manager": manager, "pending": pending, "security_pending": security,
        "reboot_required": reboot, "refresh_ok": refreshed,
        "images_checked": len(imgs), "images_outdated": outdated,
    }, message="%d updates pending (%d security), %d of %d images outdated%s"
               % (pending, security, outdated, len(imgs),
                  ", reboot required" if reboot else "")))

    if "--stdout" in sys.argv:
        for d in docs:
            print(json.dumps(d))
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a") as fh:
        for d in docs:
            fh.write(json.dumps(d, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
