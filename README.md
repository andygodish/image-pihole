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

The upstream healthcheck should become healthy. If a production port preflight
reports `Permission denied` for port 53, run `sudo make up` on the Docker host:
the current preflight tests port binding directly and therefore needs permission
to bind privileged ports. This error alone does not mean the port is occupied.

On that Docker host:

```sh
dig @127.0.0.1 -p 1053 _kerberos._udp.home.arpa SRV +short
dig @127.0.0.1 -p 1053 example.com A +short
curl -I http://127.0.0.1:8080/admin/
```

Expect `freeipa.home.arpa.` port 88, public DNS answers, and a web response.
On a remote host, use an SSH tunnel for the loopback-bound admin interface.
For the current Docker host (`192.168.1.202`), run on the Mac:

```sh
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ecdsa -N -L 18080:127.0.0.1:8080 ubuntu@192.168.1.202
```

Keep that terminal open and visit `http://127.0.0.1:18080/admin/`. Log in with
the password saved in `.secrets/pihole_password`. The operator confirmed login
through this tunnel succeeded.

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

Compose also sets `FTLCONF_dns_domain_name` to `FREEIPA_DOMAIN` and
`FTLCONF_dns_domain_local` to `false`. These settings allow Pi-hole v6 to
forward the domain instead of generating the conflicting `local=/home.arpa/`
rule. The domain-specific server rule above keeps those queries directed to
FreeIPA. All three settings are required for this fresh-deployment setup.

The first runtime check returned `NXDOMAIN` despite the correct forwarding
rule and a successful direct FreeIPA query. The operator set
`dns.domain.name=home.arpa` and `dns.domain.local=false` through the FTL CLI,
then restarted Pi-hole. Those manual settings persist in that named volume;
Compose now supplies them for new volumes too. The post-fix query through
Pi-hole returned `0 100 88 freeipa.home.arpa.`, confirming Kerberos SRV
discovery through the domain-specific forwarder. The external query through
Pi-hole returned `172.66.147.243` and `104.20.23.154`; Compose reported the
container healthy. Admin login through the SSH tunnel passed. The operator
chose to recreate required DNS records instead of importing the old deployment.
They added `freeipa.home.arpa -> 192.168.1.125` through Settings > Local DNS
Records, and the reverse query through Pi-hole returned `freeipa.home.arpa.`.
The operator changed `.env` to `DNS_BIND_IP=192.168.1.202` and `DNS_PORT=53`,
then recreated the container with the same named volume. The Python preflight
could not bind privileged port 53 as the ordinary Ubuntu user; `sudo make up`
completed the check and started the service. From the Mac, both Kerberos SRV
and external A queries to `192.168.1.202:53` passed. FreeIPA still forwards
external queries to the old `.14` at that checkpoint. The operator subsequently
changed FreeIPA's per-server forwarder to `.202` (policy `only`) and verified
public A resolution through FreeIPA. UniFi LAN DHCP now advertises only `.202`;
both old DNS addresses were removed. The Mac received `.202` after DHCP renewal.

Direct A queries from the Mac to Pi-hole return `192.168.1.125`. Native macOS
lookups still fail for `freeipa.home.arpa`, while external names resolve. Cache
clearing did not help; the failed native lookup did not appear in the Pi-hole
query log. Inspection identified a managed DNS-over-HTTPS profile on the work
Mac. This is the leading explanation, not a confirmed traffic trace. No managed
profile was modified. Confirm normal resolution on an unmanaged client and
coordinate any local-domain DNS exception with the profile owner.

During comparison testing, the operator temporarily recreated `uds.dev` and
`sso.uds.dev` records from the old resolver, then identified possible company
use of `uds.dev` and removed both records from the new Pi-hole. No `uds.dev`
record or wildcard is part of the intended deployment. Leave company-domain
configuration unchanged; FreeIPA continues to use `home.arpa`. Any future
homelab Kubernetes domain migration is a separate task, including ingress and
application configuration. The Docker VM's own resolver initially retained `.14`, `.11`, `1.1.1.1` and
`8.8.8.8` in merged installer Netplan files. The operator removed saved
nameservers from both `50-cloud-init.yaml` and `00-installer-config.yaml`,
corrected the former's permissions to 0600, and confirmed `netplan try`.
`resolvectl status ens18` then showed only `.202`; a normal system lookup with
`getent ahostsv4 freeipa.home.arpa` returned `192.168.1.125`.
`getent ahostsv4 example.com` also returned the expected public addresses,
confirming native external resolution after the Netplan repair. The managed
work Mac's native-resolution issue remains separate and unresolved.

The Ubuntu template had disabled cloud-init networking. Source fixes in
`packer-ubuntu-2604` remove that disable file and installer Netplan settings at
cleanup; `tofu-proxmox` now explicitly supplies Ubuntu DHCP and optional DNS
settings. These source fixes have been validated but not rebuilt/applied to
infrastructure. Existing VM configuration was repaired manually as above.

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

## Optional browser access through Caddy

For `https://pihole.home.arpa/admin/`, add the local A record
`pihole.home.arpa -> 192.168.1.202`. With their `docker-compose.proxy.yaml` overlays enabled, Pi-hole and
`image-caddy` join the same external Docker network, `homelab-proxy`, on the
`.202` host. Create that network once before starting this optional integration:

```sh
docker network inspect homelab-proxy >/dev/null 2>&1 || docker network create homelab-proxy
```

The Caddy route is `reverse_proxy pihole-new:80` with `tls internal` in its
local Caddyfile. See the [image-caddy integration guide](https://github.com/andygodish/image-caddy#optional-pi-hole-integration-on-the-same-docker-host) for the route, validation and
application steps. Update both repositories before enabling this route.
Enable the optional network with:

```sh
docker compose -f docker-compose.yaml -f docker-compose.proxy.yaml config --quiet
docker compose -f docker-compose.yaml -f docker-compose.proxy.yaml up -d --no-deps pihole
```

Use the corresponding overlay in `image-caddy` too. A restart alone does not
attach an existing container to the new network. Recreating the
container briefly interrupts DNS and preserves the named data volume and secret.
No image rebuild is required.

The admin host binding stays on `127.0.0.1:8080`; the SSH tunnel remains an
optional recovery path. Normal access uses Caddy's existing LAN ports 80/443.
Devices need working LAN DNS and trust in Caddy's local CA. The work Mac's
managed DNS remains a separate unresolved issue. Caddy can continue serving its
other routes when Pi-hole is stopped; Pi-hole can run independently of Caddy
as long as the external network exists. Neither project removes that network
when brought down. DNS bindings and FreeIPA forwarding are unchanged.

### Why an optional shared network?

The base `docker-compose.yaml` runs this service independently. Ordinary
`docker compose up -d` or `make up` does not require `homelab-proxy`, a sibling
repository, or the other container. Image builds and image runtime defaults do
not depend on this network. Pi-hole's existing FreeIPA DNS defaults are a
separate homelab setting, documented above.

Each Compose project normally has its own private default bridge. A container
on one project's default bridge cannot discover the other project's service
through Docker DNS. Also, `127.0.0.1` inside Caddy refers to Caddy itself, not
the Docker host or Pi-hole. The optional shared bridge gives Caddy a stable
Docker name, `pihole-new`, through which it can reach Pi-hole's internal HTTP
port 80. No fixed container IP or additional LAN web port is needed.

This bridge exists only inside one Docker host. It is not a UniFi VLAN, a LAN
DNS zone, or a replacement for Pi-hole's DNS service. Browser requests still
resolve `pihole.home.arpa` to the host's LAN IP and connect to Caddy on 443.
Caddy then makes a separate HTTP request over the bridge to Pi-hole. DNS
queries from LAN clients still use Pi-hole's published host port 53. The
`.202` address and `pihole.home.arpa` name describe this homelab, not required
values for another developer.

The overlay is optional because another deployment may run only one service,
use another reverse proxy, or put the services on different machines. For
separate machines, configure a reachable, appropriately protected backend
address instead; this single-host bridge cannot connect them. A standalone
Pi-hole installation can use its published admin port, and standalone Caddy
can proxy any configured reachable backend.

When this integration is enabled, create `homelab-proxy` once on the Docker
host. `external: true` means the operator owns its lifecycle: neither Compose
project creates or deletes it. The services have no cross-project startup
ordering or `depends_on`. Caddy starts without Pi-hole, but requests to the
Pi-hole route return 502 while the backend is unavailable. Pi-hole continues
serving DNS without Caddy. Only trusted proxy backends should share this
network, since members can reach each other's container ports.

Keep including both `-f` files when updating the integrated deployment.
Running plain `make up` or base-only `docker compose up -d` reconciles it back
to standalone networking and removes the shared connection, breaking the
proxy route. To intentionally return to standalone operation, remove the
Pi-hole route from Caddy's local Caddyfile first, then run base-only
`docker compose up -d` in each project. Named volumes and the external network
are retained; do not use `down -v`.

See [Docker's cross-project networking documentation](https://docs.docker.com/compose/how-tos/networking/#connecting-multiple-compose-projects).
