#!/usr/bin/python3
"""Wayne's stopped-volume backup. Run --check before enabling the timer."""

import argparse
import datetime
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time


CONTAINER = "pihole-new"
VOLUME = "image-pihole_pihole_data"
SOURCE = "192.168.1.55:/pihole.home.arpa"


def run(*args, timeout=60):
    return subprocess.check_output(args, text=True, timeout=timeout).strip()


def inspect():
    return json.loads(run("docker", "inspect", CONTAINER))[0]


def git(repo, *args):
    # The installed systemd job runs as root; the checkout belongs to pi.
    return run("git", "-c", f"safe.directory={repo}", "-C", str(repo), *args)


def check_mount(destination):
    # Access triggers systemd's automount. Reject autofs and local fallbacks.
    run("timeout", "30", "stat", str(destination / "."), timeout=35)
    entries = json.loads(run("findmnt", "-J", "-T", str(destination),
                             "-o", "SOURCE,FSTYPE,TARGET"))["filesystems"]
    if not any(e["source"] == SOURCE and e["fstype"] in ("nfs", "nfs4")
               and e["target"] == str(destination) for e in entries):
        raise RuntimeError("Expected QNAP NFS export is not mounted; refusing local fallback")


def preflight(repo, destination, staging):
    for tool in ("docker", "tar", "git", "dig", "findmnt", "timeout", "stat", "du"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing command: {tool}")
    for name in (".env", ".secrets/pihole_password"):
        if not (repo / name).is_file() or not (repo / name).stat().st_size:
            raise RuntimeError(f"Missing or empty {repo / name}")
    git(repo, "rev-parse", "HEAD")
    state = inspect()
    if not state["State"]["Running"]:
        raise RuntimeError("Pi-hole must already be running; refusing a backup during maintenance")
    mounts = [m for m in state["Mounts"] if m["Destination"] == "/etc/pihole"
              and m["Type"] == "volume" and m["Name"] == VOLUME]
    if len(mounts) != 1:
        raise RuntimeError("Unexpected /etc/pihole mount")
    volume_path = Path(mounts[0]["Source"])
    check_mount(destination)
    # Both probes happen before any Docker stop operation.
    for directory in (staging, destination):
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b"Wayne backup write test\n")
            probe.flush()
            os.fsync(probe.fileno())
    size = int(run("du", "-sb", str(volume_path)).split()[0])
    # Uncompressed archive and compressed output coexist during compression.
    required = size * 3 + 256 * 1024 * 1024
    for directory in (staging, destination):
        if shutil.disk_usage(directory).free < required:
            raise RuntimeError(f"Insufficient free space in {directory}: need {required} bytes")
    return state, volume_path


def capture_volume(volume_path, archive):
    # Set the restart guard BEFORE stop: even a timed-out stop needs recovery.
    try:
        print("Stopping Pi-hole for local volume capture", flush=True)
        run("docker", "stop", "--time", "30", CONTAINER)
        stopped = inspect()["State"]
        if stopped["Running"]:
            raise RuntimeError("Container did not stop")
        if stopped.get("ExitCode") == 137:
            raise RuntimeError("Pi-hole was killed rather than stopped gracefully; refusing recovery point")
        run("tar", "--numeric-owner", "--acls", "--xattrs", "-cpf", str(archive),
            "-C", str(volume_path), ".", timeout=300)
    finally:
        print("Starting Pi-hole", flush=True)
        run("docker", "start", CONTAINER)


def check_dns(state):
    # Use the actual published endpoint, not a separately maintained IP setting.
    bindings = state["HostConfig"]["PortBindings"]
    for protocol in ("udp", "tcp"):
        binding = bindings[f"53/{protocol}"][0]
        address = binding["HostIp"] or "127.0.0.1"
        if address == "0.0.0.0":
            address = "127.0.0.1"
        elif address == "::":
            address = "::1"
        for attempt in range(15):
            args = ["dig", f"@{address}", "-p", binding["HostPort"],
                    "example.com", "+time=2", "+tries=1"]
            if protocol == "tcp":
                args.append("+tcp")
            try:
                if "status: NOERROR" in run(*args, timeout=5):
                    break
            except (subprocess.SubprocessError, OSError):
                pass
            if attempt == 14:
                raise RuntimeError(f"Pi-hole {protocol} DNS check failed")
            time.sleep(2)
    print("UDP and TCP DNS checks passed", flush=True)


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def publish(local, destination):
    check_mount(destination)
    remote = destination / "wayne"
    remote.mkdir(mode=0o700, exist_ok=True)
    partial = remote / ("." + local.name + ".partial")
    partial.mkdir(mode=0o700)
    # Incomplete transfers remain visibly .partial, never a completed set.
    for path in sorted(local.iterdir()):
        target = partial / path.name
        with path.open("rb") as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        if digest(path) != digest(target):
            raise RuntimeError(f"NAS checksum mismatch: {path.name}")
    final = remote / local.name
    partial.rename(final)
    return final


def backup(repo, destination, staging, state, volume_path):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    local = Path(tempfile.mkdtemp(prefix=stamp + "-", dir=staging))
    print(f"Local recovery set: {local}", flush=True)
    # Keep failed sets for investigation and manual recovery; never prune here.
    manifest = {
        "created_utc": stamp, "hostname": socket.gethostname(), "volume": VOLUME,
        "git_commit": git(repo, "rev-parse", "HEAD"),
        "git_status": git(repo, "status", "--porcelain"),
        "image_reference": state["Config"]["Image"], "image_id": state["Image"],
        "image_digests": json.loads(run("docker", "image", "inspect", state["Image"],
                                         "--format", "{{json .RepoDigests}}")),
    }
    (local / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    run("tar", "--numeric-owner", "--acls", "--xattrs", "-czpf", str(local / "config.tar.gz"),
        "-C", str(repo), ".env", ".secrets")
    raw = local / "pihole-data.tar"
    stopped_at = time.monotonic()
    capture_volume(volume_path, raw)
    check_dns(state)
    manifest["stop_through_dns_validation_seconds"] = round(time.monotonic() - stopped_at, 2)
    (local / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    # Compression and all NAS writes happen after Pi-hole has restarted.
    with raw.open("rb") as source, gzip.open(local / "pihole-data.tar.gz", "wb", compresslevel=1) as target:
        shutil.copyfileobj(source, target)
    raw.unlink()
    run("tar", "-tzf", str(local / "pihole-data.tar.gz"), timeout=300)
    checksums = "".join(f"{digest(p)}  {p.name}\n" for p in sorted(local.iterdir()))
    (local / "SHA256SUMS").write_text(checksums)
    final = publish(local, destination)
    shutil.rmtree(local)
    print(f"Backup complete and NAS checksums verified: {final}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check prerequisites without stopping Pi-hole")
    parser.add_argument("--repo", type=Path, default=Path("/home/pi/src/github.com/andygodish/image-pihole"))
    parser.add_argument("--destination", type=Path, default=Path("/mnt/pihole-backups"))
    parser.add_argument("--staging", type=Path, default=Path("/var/lib/pihole-backup"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run with sudo")
    os.umask(0o077)
    args.staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.staging.chmod(0o700)
    with (args.staging / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state, volume_path = preflight(args.repo, args.destination, args.staging)
        check_dns(state)
        print("Preflight passed; Pi-hole is still running", flush=True)
        if not args.check:
            backup(args.repo, args.destination, args.staging, state, volume_path)


def interrupted(signum, frame):
    # A graceful service stop still passes through capture_volume's finally.
    raise RuntimeError(f"Interrupted by signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError, KeyError, ValueError) as error:
        print(f"Backup failed: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
