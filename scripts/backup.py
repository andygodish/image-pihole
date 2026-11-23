#!/usr/bin/python3
"""Pi-hole stopped-volume backup. Run --check before enabling the timer."""

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


def load_config(path):
    values = json.loads(path.read_text())
    required = {"container", "volume", "source", "repo", "destination", "staging"}
    if not isinstance(values, dict) or set(values) != required:
        raise ValueError("Backup config must contain exactly: " + ", ".join(sorted(required)))
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise ValueError("Backup config values must be nonempty strings")
    for key in ("repo", "destination", "staging"):
        values[key] = Path(values[key])
        if not values[key].is_absolute():
            raise ValueError(f"Backup config {key} must be an absolute path")
    return argparse.Namespace(**values)


def run(*args, timeout=60):
    return subprocess.check_output(args, text=True, timeout=timeout).strip()


def inspect(container):
    return json.loads(run("docker", "inspect", container))[0]


def git(repo, *args):
    # Root must not refresh/replace the checkout owner's index during status.
    return run("git", "--no-optional-locks", "-c", f"safe.directory={repo}",
               "-C", str(repo), *args)


def check_mount(destination, expected_source):
    # Access triggers systemd's automount. Reject autofs and local fallbacks.
    run("timeout", "30", "stat", str(destination / "."), timeout=35)
    entries = json.loads(run("findmnt", "-J", "-T", str(destination),
                             "-o", "SOURCE,FSTYPE,TARGET"))["filesystems"]
    if not any(e["source"] == expected_source and e["fstype"] in ("nfs", "nfs4")
               and e["target"] == str(destination) for e in entries):
        raise RuntimeError("Expected NFS export is not mounted; refusing local fallback")


def preflight(config):
    repo, destination, staging = config.repo, config.destination, config.staging
    for tool in ("docker", "tar", "git", "dig", "findmnt", "timeout", "stat", "du"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing command: {tool}")
    for name in (".env", ".secrets/pihole_password"):
        if not (repo / name).is_file() or not (repo / name).stat().st_size:
            raise RuntimeError(f"Missing or empty {repo / name}")
    git(repo, "rev-parse", "HEAD")
    state = inspect(config.container)
    if not state["State"]["Running"]:
        raise RuntimeError("Pi-hole must already be running; refusing a backup during maintenance")
    mounts = [m for m in state["Mounts"] if m["Destination"] == "/etc/pihole"
              and m["Type"] == "volume" and m["Name"] == config.volume]
    if len(mounts) != 1:
        raise RuntimeError("Unexpected /etc/pihole mount")
    volume_path = Path(mounts[0]["Source"])
    check_mount(destination, config.source)
    # Both probes happen before any Docker stop operation.
    for directory in (staging, destination):
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b"Pi-hole backup write test\n")
            probe.flush()
            os.fsync(probe.fileno())
    size = int(run("du", "-sb", str(volume_path)).split()[0])
    # Uncompressed archive and compressed output coexist during compression.
    required = size * 3 + 256 * 1024 * 1024
    for directory in (staging, destination):
        if shutil.disk_usage(directory).free < required:
            raise RuntimeError(f"Insufficient free space in {directory}: need {required} bytes")
    return state, volume_path


def capture_volume(volume_path, archive, container):
    # Set the restart guard BEFORE stop: even a timed-out stop needs recovery.
    try:
        print("Stopping Pi-hole for local volume capture", flush=True)
        run("docker", "stop", "--time", "30", container)
        stopped = inspect(container)["State"]
        if stopped["Running"]:
            raise RuntimeError("Container did not stop")
        if stopped.get("ExitCode") == 137:
            raise RuntimeError("Pi-hole was killed rather than stopped gracefully; refusing recovery point")
        run("tar", "--numeric-owner", "--acls", "--xattrs", "-cpf", str(archive),
            "-C", str(volume_path), ".", timeout=300)
    finally:
        print("Starting Pi-hole", flush=True)
        run("docker", "start", container)


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


def publish(local, destination, expected_source):
    check_mount(destination, expected_source)
    remote = destination
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


def backup(config, state, volume_path):
    repo, destination, staging = config.repo, config.destination, config.staging
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    local = Path(tempfile.mkdtemp(prefix=stamp + "-", dir=staging))
    print(f"Local recovery set: {local}", flush=True)
    # Keep failed sets for investigation and manual recovery; never prune here.
    manifest = {
        "created_utc": stamp, "hostname": socket.gethostname(), "volume": config.volume,
        "backup_config": {key: str(value) for key, value in vars(config).items()},
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
    capture_volume(volume_path, raw, config.container)
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
    final = publish(local, destination, config.source)
    shutil.rmtree(local)
    print(f"Backup complete and NAS checksums verified: {final}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check prerequisites without stopping Pi-hole")
    parser.add_argument("--config", type=Path, default=Path("/etc/pihole-backup.json"),
                        help="Deployment settings (default: /etc/pihole-backup.json)")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run with sudo")
    config = load_config(args.config)
    os.umask(0o077)
    config.staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.staging.chmod(0o700)
    with (config.staging / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state, volume_path = preflight(config)
        check_dns(state)
        print("Preflight passed; Pi-hole is still running", flush=True)
        if not args.check:
            backup(config, state, volume_path)


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
