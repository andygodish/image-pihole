#!/usr/bin/env python3
"""Check TCP/UDP publish addresses without starting or stopping containers."""
import json
import socket
import subprocess

config = json.loads(subprocess.check_output(
    ["docker", "compose", "-f", "docker-compose.yaml", "config", "--format", "json"], text=True))
service = config["services"]["pihole"]
# Do not permit an unrelated container with a matching name to bypass checks.
inspection = subprocess.run(["docker", "inspect", service["container_name"]],
                            text=True, capture_output=True)
if inspection.returncode == 0:
    container = json.loads(inspection.stdout)[0]
    labels = container["Config"].get("Labels") or {}
    if labels.get("com.docker.compose.project") != config["name"]:
        raise SystemExit("Container name is already used by another deployment.")
    if container["State"]["Running"]:
        raise SystemExit("Stack is already running. Use make down before changing published ports, or make restart.")
for port in service["ports"]:
    address = port.get("host_ip", "0.0.0.0")
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    kind = socket.SOCK_DGRAM if port["protocol"] == "udp" else socket.SOCK_STREAM
    with socket.socket(family, kind) as listener:
        try:
            listener.bind((address, int(port["published"])))
        except OSError as error:
            raise SystemExit(f"Cannot bind {address}:{port['published']}/{port['protocol']}: {error}")
print("Configured DNS TCP/UDP and web ports are available.")
