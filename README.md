# Homelab Pi-hole image

A thin wrapper around the official Pi-hole v6 Docker image, matching the sibling
`image-*` projects: pinned upstream, Makefile, named volumes, Renovate, upver,
branch builds and multi-architecture GHCR releases.

The upstream image version is pinned in `Dockerfile`. Local releases use
`v<upstream>-<revision>` tags at `ghcr.io/andygodish/pihole`.

`version.txt` starts at the unreleased bootstrap value `0.0.0-0`. On the first
main push, upver compares it with `ARG PIHOLE_VERSION="2026.10.0"` and proposes
`2026.10.0-0` in a release PR. Do not pre-seed the intended release version:
that would cause upver to propose `-1` instead. Only the Dockerfile's upstream
ARG is the base image version; `version.txt` identifies this wrapper's release.

## Runtime contract

- Preserve upstream entrypoint, command and healthcheck. Initialization and cron
  start as root; the entrypoint runs FTL as `pihole`, UID/GID `10006:10006`.
  This is an explicit exception to a container-wide non-root `USER`: setting
  `USER` would break upstream initialization and capability handling.
- `/etc/pihole` uses the project-scoped named volume `image-pihole_pihole_data`.
  `make down` preserves it. Never use `down -v` for ordinary upgrades.
- No DHCP, NTP server, host networking, privileged mode or added NET_ADMIN/SYS_TIME
  capabilities. UniFi remains DHCP server; the host keeps its own time service.
- DNS defaults to loopback port 1053, and HTTP admin to loopback port 8080.
  LAN exposure and the eventual replacement address must be chosen at cutover.
- Credentials come from an ignored local file through a Compose secret.
  Local Compose secrets are file mounts, not an encrypted secret store.
- DNS providers and FreeIPA addressing belong in Compose/.env, not the image.

## Prepare and validate

Requires Docker Engine, Compose v2, Make and Python 3. These commands are for the
operator to run on the chosen Docker host. Nothing is deployed by creating this repo.

Copy the non-secret configuration:

```sh
cp .env.example .env
```

Create the secret directory and file without putting a password in shell history:

```sh
mkdir -p .secrets
chmod 700 .secrets
bash -c 'umask 077; read -r -s -p "Pi-hole admin password: " password; printf "\n"; test -n "$password" && printf "%s" "$password" > .secrets/pihole_password'
```

Save that password in your password manager. Review `.env`, including external
upstreams, then validate, build and check the image:

```sh
make config
make build
make test
```

Start a separate local test instance using `docker-compose.local.yaml` and the
`pihole:local` build tag (works even before the first GHCR release):

```sh
make up-local
make status
```

The upstream healthcheck should become healthy. On that Docker host:

```sh
dig @127.0.0.1 -p 1053 _kerberos._udp.home.arpa SRV +short
dig @127.0.0.1 -p 1053 example.com A +short
curl -I http://127.0.0.1:8080/admin/
```

Expect `freeipa.home.arpa.` port 88, public DNS answers, and a web response.
On a remote host, use an SSH tunnel for the loopback-bound admin interface.
`make test` is a binary/defaults check; live DNS and web behavior require these
separate operator checks. To change ports on a running stack, use `make down`,
edit `.env`, then start it again. `make restart` does not apply Compose changes.

After publishing the initial release, `make up` uses the pinned GHCR tag.
After the first upver release is merged, `make build-multiarch` publishes both
the version tag and `latest`; it is an
explicit publishing action, separate from normal local builds.

## DNS and FreeIPA

Compose injects this v6 dnsmasq line via `FTLCONF_misc_dnsmasq_lines`:

```ini
server=/home.arpa/192.168.1.125
```

Only `home.arpa` goes to FreeIPA. External queries use `PIHOLE_UPSTREAMS`.
Never set FreeIPA as the global upstream while FreeIPA forwards external queries
back to Pi-hole: that creates a loop. Do not forward the LAN reverse zone to
FreeIPA; it does not host one. Preserve the existing PTR for
`192.168.1.125 -> freeipa.home.arpa` in Pi-hole during migration.

Environment-managed v6 settings are read-only in the web UI. Change `.env` or
Compose and recreate the service to change them. There is no custom dnsmasq
file mount required for a fresh deployment. Review any imported `local=`,
`address=`, conditional-forwarding or local-zone settings for conflicts.

## Migration and rollback: not performed yet

The existing service is Pi-hole v5.18.2 / FTL v5.25.1 / Web v5.21, Docker tag
2024.03.2, on `192.168.1.14`. `.11` is retired. FreeIPA uses `.14` for external
DNS and already has working integrated DNS, Kerberos, LDAP/StartTLS and HTTPS.

1. Export a Teleporter backup through the old web UI. Recover and save its
   original Compose definition, environment, mount locations and image tag
   before cutover. Record local A/PTR/CNAME entries, lists, client groups,
   upstream providers and any custom dnsmasq configuration. Treat backups as
   sensitive. Do not delete the old container or its data.
2. Validate the new instance separately on test ports and its own named volume.
   Review the v5-to-v6 migration guide before importing supported Teleporter data
   through the new UI. Do not point v6 at the live v5 data directory. Recheck all
   DNS tests and imported local/reverse records after import.
3. Choose the actual Docker host and production address. Keeping `.14` minimizes
   changes because FreeIPA forwards there. Bind DNS TCP/UDP port 53 to the
   selected LAN IP; keep admin loopback-bound unless LAN access is intentional.
   Do not assume `.14` can be bound on a different host without moving that IP.
4. Schedule a short DNS outage, stop the old service without deleting its data,
   and start the new service on the agreed production bindings. A local port
   preflight cannot prove that a LAN address is free on another host.
5. Test A, SRV, PTR, external DNS and admin login through the production IP, from
   both a LAN client and FreeIPA. Confirm the router advertises only the active
   resolver. If the Pi-hole IP changes, update FreeIPA's forwarder before relying
   on the new address, and review DHCP DNS separately.
6. For rollback, stop the new service and restart the old Compose service with
   its original image and untouched data at the old address. Never downgrade v6
   data in place into v5. Keep the old export and deployment until migration is verified.

FreeIPA follow-up is paused in `../tofu-proxmox/docs/freeipa/clickops.md` until
this DNS migration is complete. CA recovery backup and browser login remain pending.

## Automation and releases

`version.txt`, `upver.yaml`, `CHANGELOG.md` and the release workflow follow the
sibling projects. Main pushes open/update a `release/upver` PR; a version-file
change publishes a git tag and amd64/arm64 images. As with the sibling repos,
GitHub Actions must be allowed to create pull requests and write packages.
In Settings > Actions > General, enable **Allow GitHub Actions to create and
approve pull requests**. Install/enable the Renovate GitHub App for this repo.
Create and push the GitHub repository from an authenticated machine.
Renovate tracks the official image ARG, action dependencies, upver, the validator
image and the published Compose tag (via this project's git tags). Only custom
regex and GitHub Actions managers are enabled, avoiding duplicate Dockerfile or
Compose detection. Wrapper revisions use a numeric `build` component, so `-1`
is newer than `-0`, rather than a prerelease/compatibility label.

Release sequence:

1. Push the bootstrap project to `main`: CI validates/builds/tests; upver opens
   the first release PR with `2026.10.0-0` and its generated changelog.
2. Merge that PR: the release workflow validates/builds/tests again, publishes
   amd64/arm64 images, then creates git tag `v2026.10.0-0`.
3. Clone or pull `main` on the Docker VM. `make build` builds locally from the
   pinned official Pi-hole Dockerfile image; `make up-local` uses that local
   build. Alternatively `make up` pulls the published wrapper image.
4. When Renovate updates the upstream Dockerfile ARG, upver resets the revision
   to `-0`. Changes with an unchanged upstream increment the revision (`-1`,
   `-2`, ...). Updating the pending release PR recomputes from `main` so it
   does not consume revisions before release. Compose-only changes skip the
   release workflow, preventing Renovate's deployment-pin updates from creating
   an endless sequence of new image releases.

See [first GitHub publication](docs/publish.md) for repository bootstrap.

Inspect automation locally with `make renovate-validate` and
`make renovate-dry-run`. The latter performs dependency extraction without
creating PRs. Pass `RENOVATE_TOKEN` for authenticated GitHub dependency checks;
without it, extraction may report token-required skips and return nonzero.
GitHub App access and the first hosted Renovate run must still
be verified after the repo exists.

References:

- [Official image releases](https://github.com/pi-hole/docker-pi-hole/releases)
- [Docker configuration and secrets](https://docs.pi-hole.net/docker/configuration/)
- [v6 DNS configuration](https://docs.pi-hole.net/ftldns/configfile/)
- [v5-to-v6 migration](https://docs.pi-hole.net/docker/upgrading/v5-v6/)
- [Upstream startup and privilege dropping](https://github.com/pi-hole/docker-pi-hole/blob/2026.10.0/src/start.sh)
