# renovate: datasource=docker depName=pihole/pihole
ARG PIHOLE_VERSION="2026.10.0"
FROM pihole/pihole:${PIHOLE_VERSION}

LABEL org.opencontainers.image.title="Homelab Pi-hole"
LABEL org.opencontainers.image.description="Pinned Pi-hole runtime for homelab DNS."
LABEL org.opencontainers.image.source="https://github.com/andygodish/image-pihole"

# Preserve upstream root initialization, entrypoint, command and healthcheck.
# The entrypoint drops FTL to the pihole user; do not set USER here.
ENV PIHOLE_UID=10006 \
    PIHOLE_GID=10006 \
    DNSMASQ_USER=pihole

COPY version.txt /usr/local/share/version.txt
VOLUME ["/etc/pihole"]
EXPOSE 53/tcp 53/udp 80/tcp
