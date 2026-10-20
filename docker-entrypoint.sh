#!/bin/sh
set -e

# TIMEZONE is kept for compatibility with the original image
if [ -n "${TIMEZONE}" ] && [ -z "${TZ}" ] && [ -f "/usr/share/zoneinfo/${TIMEZONE}" ]; then
    export TZ="${TIMEZONE}"
fi

# The original image used DOCKER_ENTRYPOINT for the local socket
if [ -z "${DOCKER_HOST}" ] && [ -n "${DOCKER_ENTRYPOINT}" ]; then
    export DOCKER_HOST="${DOCKER_ENTRYPOINT}"
fi

TCC_USER="${TCC_USER:-root}"
LOG_PATH="${LOG_PATH:-/logs}"

if [ "$(id -u)" = "0" ] && [ "${TCC_USER}" != "root" ]; then
    mkdir -p "${LOG_PATH}"
    chown "${TCC_USER}" "${LOG_PATH}" 2>/dev/null || true
    socket="${DOCKER_HOST:-unix:///var/run/docker.sock}"
    case "${socket}" in
        unix://*)
            socket="${socket#unix://}"
            case "${socket}" in /*) ;; *) socket="/${socket}" ;; esac
            if [ -S "${socket}" ] && ! su-exec "${TCC_USER}" test -r "${socket}"; then
                # Give the user the socket's group, since su-exec drops groups added with --group-add
                gid="$(stat -c %g "${socket}")"
                group="$(getent group "${gid}" | cut -d: -f1)"
                if [ -z "${group}" ]; then
                    addgroup -S -g "${gid}" docker-host
                    group=docker-host
                fi
                addgroup "${TCC_USER}" "${group}" 2>/dev/null || true
                if ! su-exec "${TCC_USER}" test -r "${socket}"; then
                    echo "WARNING: user '${TCC_USER}' can't read ${socket}; run as root or use a socket proxy" >&2
                fi
            fi
            ;;
    esac
    exec su-exec "${TCC_USER}" "$@"
fi

exec "$@"
