PIHOLE_VERSION ?= $(shell sed -n 's/^ARG PIHOLE_VERSION="\([^"]*\)"/\1/p' Dockerfile)
VERSION_TAG    ?= $(shell test ! -s version.txt || sed 's/^v//' version.txt | tr -d '[:space:]' | sed 's/^/v/')
REGISTRY       := ghcr.io/andygodish
IMAGE_NAME     := pihole
FULL_TAG       := $(REGISTRY)/$(IMAGE_NAME):$(VERSION_TAG)
LATEST_TAG     := $(REGISTRY)/$(IMAGE_NAME):latest
REPO_ROOT      := $(CURDIR)
PLATFORMS      ?= linux/amd64,linux/arm64
RENOVATE_IMAGE ?= renovate/renovate:44.257.9
COMPOSE        := docker compose

.DEFAULT_GOAL := help
.PHONY: pull proxy-network verify-env verify-release build build-multiarch config verify-runtime verify-ports fix-permissions up run up-local down stop restart logs status ps test backup-check backup-install backup-test renovate-validate renovate-dry-run help

verify-env:
	@test -s version.txt || { echo "Missing version.txt"; exit 1; }

build: verify-env
	docker build --build-arg PIHOLE_VERSION=$(PIHOLE_VERSION) -t $(IMAGE_NAME):$(VERSION_TAG) -t $(IMAGE_NAME):local -t $(FULL_TAG) .

verify-release: verify-env
	@printf '%s\n' '$(VERSION_TAG)' | grep -Eq '^v[0-9]{4}\.[0-9]{2}\.[0-9]+-[0-9]+$$' || { echo "Publish only after upver creates a release version."; exit 1; }

build-multiarch: verify-release
	docker buildx build --platform $(PLATFORMS) --build-arg PIHOLE_VERSION=$(PIHOLE_VERSION) -t $(FULL_TAG) -t $(LATEST_TAG) --push .

config:
	$(COMPOSE) config --quiet

verify-runtime: config
	@test -s .secrets/pihole_password || { echo "Create a nonempty .secrets/pihole_password first; see README.md."; exit 1; }

verify-ports: verify-runtime
	python3 scripts/verify-ports.py

fix-permissions:
	@echo "Upstream initializes named-volume ownership and runs FTL as pihole (10006:10006)."

up: proxy-network verify-ports fix-permissions
	$(COMPOSE) up -d --pull missing

run: up

# Use the image built by make build without requiring an initial GHCR release.
up-local: build verify-ports fix-permissions
	$(COMPOSE) -f docker-compose.yaml -f docker-compose.local.yaml up -d --pull never

stop down:
	$(COMPOSE) down

# Apply .env/Compose changes without tearing down the project's networks.
# Recreate even when unchanged so this remains a real restart; pull beforehand.
restart: proxy-network verify-runtime fix-permissions
	$(COMPOSE) up -d --no-deps --pull never --force-recreate pihole

logs:
	$(COMPOSE) logs -f --tail=100

status ps:
	$(COMPOSE) ps

# Host-side backup installation is explicit and never enables the timer.
backup-install:
	python3 scripts/backup.py --env-file "$(REPO_ROOT)/.env" --write-config /etc/pihole-backup.json
	install -m 0755 -o root -g root scripts/backup.py /usr/local/sbin/pihole-backup
	install -m 0644 -o root -g root systemd/pihole-backup.service systemd/pihole-backup.timer /etc/systemd/system/
	systemctl daemon-reload

backup-check:
	python3 scripts/backup.py --check --env-file "$(REPO_ROOT)/.env"

backup-test:
	python3 scripts/test_backup.py

# Checks the binary and runtime defaults without starting the service or publishing ports.
test: verify-env
	docker run --rm --network none --entrypoint /bin/bash $(IMAGE_NAME):$(VERSION_TAG) -ec 'pihole-FTL --version; test "$$PIHOLE_UID" = 10006; test "$$DNSMASQ_USER" = pihole'

renovate-validate:
	docker run --rm -v "$(REPO_ROOT):/usr/src/app:ro" -w /usr/src/app $(RENOVATE_IMAGE) renovate-config-validator --strict renovate.json

renovate-dry-run:
	docker run --rm -v "$(REPO_ROOT):/usr/src/app:ro" -w /usr/src/app -e LOG_LEVEL=debug -e RENOVATE_TOKEN $(RENOVATE_IMAGE) renovate --platform=local --dry-run=extract --onboarding=false --require-config=optional

help:
	@echo "build / build-multiarch  Build locally / publish amd64+arm64 to GHCR"
	@echo "config                  Validate Compose without starting services"
	@echo "up / up-local           Start published / locally built image"
	@echo "down / stop             Stop containers, preserving named volumes"
	@echo "restart                 Recreate Pi-hole with current config and cached image"
	@echo "logs / status           Inspect the Compose stack"
	@echo "backup-check            Check backup prerequisites without stopping DNS (sudo)"
	@echo "backup-install          Install backup script/units; timer stays disabled (sudo)"
	@echo "backup-test             Run backup safety checks without Docker/NAS access"
	@echo "test                    Check the built image's binary and runtime defaults"
	@echo "renovate-validate       Validate dependency automation configuration"
	@echo "renovate-dry-run        Inspect Renovate dependency extraction locally"

# Compose reads COMPOSE_FILE from .env; standalone users need no proxy network.
proxy-network: config
	@if $(COMPOSE) config --networks | grep -qx proxy; then \
		docker network inspect homelab-proxy >/dev/null 2>&1 || docker network create homelab-proxy; \
	fi

# Fetch updates while DNS is available, before stopping this DNS server.
pull: config
	$(COMPOSE) pull
