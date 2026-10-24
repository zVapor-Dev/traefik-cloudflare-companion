# Security policy

This container talks to your Docker socket and holds a Cloudflare API token, so security issues matter.

## Supported versions

Only the latest release gets fixes.

## Reporting a vulnerability

Please don't open a public issue. Use GitHub's private reporting instead: **Security → Report a vulnerability** on this repository. You'll get a reply within a week.

## Hardening tips

- Use a scoped Cloudflare token with only **Zone / DNS / Edit** on the zones you manage, not the Global API key.
- Pass the token as a Docker secret (`CF_TOKEN_FILE`) instead of an environment variable.
- Run as non-root with `TCC_USER=tcc`, and consider a read-only [Docker socket proxy](https://github.com/Tecnativa/docker-socket-proxy) (`DOCKER_HOST=tcp://socket-proxy:2375`, with `CONTAINERS=1`, `SERVICES=1`, `EVENTS=1`).
