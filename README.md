# traefik-cloudflare-companion

[![ci](https://github.com/zVapor-Dev/traefik-cloudflare-companion/actions/workflows/ci.yml/badge.svg)](https://github.com/zVapor-Dev/traefik-cloudflare-companion/actions/workflows/ci.yml)

> **The successor to [tiredofit/docker-traefik-cloudflare-companion](https://github.com/tiredofit/docker-traefik-cloudflare-companion).**
> The original project by Dave Conroy is no longer maintained: it has had no commits since April 2025, and its `tiredofit/traefik-cloudflare-companion` image was removed from Docker Hub. This fork continues it with the full history, and every environment variable keeps working. If you used the original, [migrating](#migrating-from-tiredofittraefik-cloudflare-companion) means changing one line.

Automatically create Cloudflare DNS records (CNAME by default) for every hostname in your Traefik `Host()` rules. It discovers hosts from Docker container labels, Docker Swarm service labels and/or the Traefik API.

## Migrating from tiredofit/traefik-cloudflare-companion

```diff
-    image: tiredofit/traefik-cloudflare-companion
+    image: ghcr.io/zvapor-dev/traefik-cloudflare-companion:8
```

Your environment variables and Docker secrets keep working. What changed:

- The image is built on the official `python:3.13-alpine`, without s6-overlay or the tiredofit base. It is about 40% smaller and stops instantly on `docker stop`.
- Options that belonged to the old base image (Zabbix, msmtp, `CONTAINER_*`) are gone. `TIMEZONE` still works.
- Domain matching is stricter: `DOMAIN1=example.com` only matches `example.com` and `*.example.com`, not `notexample.com`.

The full list is in [CHANGELOG.md](CHANGELOG.md).

## Quick start

```yaml
services:
  cloudflare-companion:
    image: ghcr.io/zvapor-dev/traefik-cloudflare-companion:8
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - TCC_USER=tcc
      - LOG_TYPE=CONSOLE
      - CF_TOKEN=your-scoped-api-token
      - TARGET_DOMAIN=host.example.org
      - DOMAIN1=example.org
      - DOMAIN1_ZONE_ID=your-zone-id
```

Any container with a label like ``traefik.http.routers.app.rule=Host(`app.example.org`)`` now gets a CNAME `app.example.org -> host.example.org`.

More examples: [compose](examples/compose.yml) and [Docker Swarm](examples/swarm-stack.yml).

### Image tags

Images are published to `ghcr.io/zvapor-dev/traefik-cloudflare-companion` for `linux/amd64` and `linux/arm64`.

| Tag                       | What it is                       |
| ------------------------- | -------------------------------- |
| `8.0.0`, `8.0`, `8`       | Releases (pin one of these)      |
| `latest`                  | The newest release               |
| `edge`                    | The latest commit on `main`      |

### Cloudflare API token

Create a [scoped API token](https://dash.cloudflare.com/profile/api-tokens) with **Zone / DNS / Edit** on the zones you list in `DOMAINn`. A Global API key also works if you set `CF_EMAIL`, but a scoped token is safer.

## Configuration

### Container options

| Parameter         | Description                                                                 | Default   |
| ----------------- | --------------------------------------------------------------------------- | --------- |
| `DRY_RUN`         | Log what would change without touching Cloudflare, `TRUE` or `FALSE`        | `FALSE`   |
| `LOG_LEVEL`       | `INFO`, `VERBOSE`, `DEBUG`, `WARNING` or `ERROR`                            | `INFO`    |
| `LOG_TYPE`        | `CONSOLE`, `FILE` or `BOTH`                                                 | `BOTH`    |
| `LOG_FILE`        | Log file name (rotated at 10 MB, 3 kept)                                    | `tcc.log` |
| `LOG_PATH`        | Log directory                                                               | `/logs/`  |
| `TCC_USER`        | User to run as. Use `tcc` to run as non-root; it gets the socket's group    | `root`    |
| `TIMEZONE` / `TZ` | Time zone for log timestamps, e.g. `Europe/Amsterdam`                       | `UTC`     |

### Docker options

| Parameter            | Description                                                                         | Default                       |
| -------------------- | ----------------------------------------------------------------------------------- | ----------------------------- |
| `DOCKER_HOST`        | Docker endpoint, e.g. `tcp://198.51.100.32:2376` or a socket proxy                  | `unix:///var/run/docker.sock` |
| `DOCKER_ENTRYPOINT`  | Old name for the local socket, still honoured when `DOCKER_HOST` is unset           |                               |
| `DOCKER_CERT_PATH`   | Client certificates for a TLS connection, e.g. `/docker-certs`                      |                               |
| `DOCKER_TLS_VERIFY`  | Verify TLS for a TCP connection                                                     |                               |
| `ENABLE_DOCKER_POLL` | Discover hosts from Docker labels, `TRUE` or `FALSE`                                | `TRUE`                        |
| `DOCKER_SWARM_MODE`  | Also discover Swarm services, `TRUE` or `FALSE` (alias `SWARM_MODE`)                | `FALSE`                       |

### Cloudflare options

| Parameter                      | Description                                                                                              | Default |
| ------------------------------ | -------------------------------------------------------------------------------------------------------- | ------- |
| `CF_EMAIL`                     | Cloudflare account email. Leave empty for a scoped token                                                 |         |
| `CF_TOKEN`                     | Scoped API token (or Global API key together with `CF_EMAIL`)                                            |         |
| `RC_TYPE`                      | Record type to create: `CNAME`, `A` or `AAAA`                                                            | `CNAME` |
| `TARGET_DOMAIN`                | Record content: a hostname for CNAME, an IP for A/AAAA                                                   |         |
| `DEFAULT_TTL`                  | TTL for records. `1` means automatic; otherwise 60-86400                                                 | `1`     |
| `REFRESH_ENTRIES`              | Overwrite existing records even when the content already matches, `TRUE` or `FALSE`                      | `FALSE` |
| `DOMAIN1`                      | First zone to manage records in                                                                          |         |
| `DOMAIN1_ZONE_ID`              | Cloudflare zone ID of `DOMAIN1`                                                                          |         |
| `DOMAIN1_PROXIED`              | Proxy records through Cloudflare, `TRUE` or `FALSE`                                                      | `FALSE` |
| `DOMAIN1_TTL`                  | TTL override for this domain                                                                             |         |
| `DOMAIN1_TARGET_DOMAIN`        | `TARGET_DOMAIN` override for this domain                                                                 |         |
| `DOMAIN1_COMMENT`              | Comment added to records created in this domain                                                          |         |
| `DOMAIN1_EXCLUDED_SUB_DOMAINS` | Comma-separated sub domains to skip. `int` skips `int.example.com` and `*.int.example.com`               |         |
| `DOMAIN2...`                   | More domains, same options                                                                               |         |

### Traefik options

| Parameter                 | Description                                                              | Default              |
| ------------------------- | ------------------------------------------------------------------------ | -------------------- |
| `TRAEFIK_VERSION`         | Label syntax: `1`, or `2` (also used for Traefik 3, `3` is accepted)     | `2`                  |
| `ENABLE_TRAEFIK_POLL`     | Poll the Traefik API for routers, `TRUE` or `FALSE`                      | `FALSE`              |
| `TRAEFIK_POLL_URL`        | Traefik API base URL, e.g. `http://traefik:8080`                         |                      |
| `TRAEFIK_POLL_SECONDS`    | Seconds between polls                                                    | `60`                 |
| `TRAEFIK_POLL_TIMEOUT`    | Request timeout in seconds                                               | `10`                 |
| `TRAEFIK_FILTER_LABEL`    | Label name (regex) to filter Docker containers and services on           | `traefik.constraint` |
| `TRAEFIK_FILTER`          | Required value (regex) of that label. Unset means no filtering           |                      |
| `TRAEFIK_INCLUDED_HOST1`  | Regex of hosts to include when polling Traefik. More with `2`, `3`, ...  | `.*`                 |
| `TRAEFIK_EXCLUDED_HOST1`  | Regex of hosts to exclude when polling Traefik. More with `2`, `3`, ...  |                      |

### Secrets

`CF_TOKEN`, `CF_EMAIL` and `DOMAINn_ZONE_ID` can be read from files instead of the environment:

- set `CF_TOKEN_FILE=/run/secrets/my_token` (any path), or
- mount a Docker secret named `CF_TOKEN` or `cf_token` in `/run/secrets/`.

## Discovery

Hosts are taken from `Host()` matchers only. `HostRegexp`, `HostSNI`, `PathPrefix` and header values in a rule are ignored. Records are only created or updated, never deleted.

### Docker

Running containers are scanned at startup, and new containers are picked up when they start.

| Traefik version | Example label                                                                          |
| --------------- | -------------------------------------------------------------------------------------- |
| `1`             | `traefik.frontend.rule=Host:a.example.com,b.example.com`                               |
| `2` / `3`       | ``traefik.http.routers.app.rule=Host(`a.example.com`) \|\| Host(`b.example.com`)``     |

### Docker Swarm

With `DOCKER_SWARM_MODE=TRUE` the companion also reads service labels (`deploy.labels` in a stack file), and catches services when they're created or updated. Swarm services can only be listed from a manager node, so place the companion on a manager.

### Traefik polling

With `ENABLE_TRAEFIK_POLL=TRUE` and `TRAEFIK_POLL_URL` set, the companion reads `/api/http/routers` every `TRAEFIK_POLL_SECONDS`. That covers routers from any provider, such as the file provider. A router counts when its status is `enabled` and its rule has a `Host()`. Its hosts then go through the include and exclude patterns, and excludes win.

`TRAEFIK_FILTER` applies to Docker labels only, not to polled routers. Use the include and exclude patterns for those.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest -q tests
docker build -t traefik-cloudflare-companion:dev .
```

## License

MIT. See [LICENSE](LICENSE). Originally written by Dave Conroy and contributors; this fork is maintained by [zVapor-Dev](https://github.com/zVapor-Dev).
