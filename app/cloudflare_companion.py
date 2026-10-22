#!/usr/bin/env python3
"""Traefik Cloudflare Companion.

Watches Docker containers, Docker Swarm services and/or the Traefik API for
Host() rules and creates matching DNS records in Cloudflare.
"""

import logging
import os
import re
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

import docker
import requests

__version__ = os.environ.get("IMAGE_VERSION", "dev")

SECRETS_DIR = "/run/secrets"
RX_DOMAIN_VAR = re.compile(r"^DOMAIN[0-9]+$", re.IGNORECASE)
RX_HOSTNAME = re.compile(r"^[A-Za-z0-9.\-]+$")
RX_V2_HOST_MATCHER = re.compile(r"\bHost\(([^)]*)\)")
RX_V2_BACKTICK = re.compile(r"`([^`]*)`")
RX_V1_RULE_LABEL = re.compile(r"traefik.*\.frontend\.rule")
RX_V2_RULE_LABEL = re.compile(r"traefik.*?\.rule")

# Mapping priorities: a host found via Docker labels wins over one found via Traefik polling
SOURCE_DOCKER = 1
SOURCE_TRAEFIK = 2

logger = logging.getLogger("cloudflare-companion")


class CloudflareAPIError(Exception):
    pass


class CloudflareClient:
    """Minimal client for the three Cloudflare DNS record calls this tool needs."""

    API_URL = "https://api.cloudflare.com/client/v4"
    RETRY_STATUSES = (429, 500, 502, 503, 504)

    def __init__(self, token, email=None, base_url=None, timeout=30, max_retries=2):
        self.base_url = (base_url or self.API_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "traefik-cloudflare-companion/{}".format(__version__)
        if email:
            self.session.headers["X-Auth-Email"] = email
            self.session.headers["X-Auth-Key"] = token
        else:
            self.session.headers["Authorization"] = "Bearer {}".format(token)

    def _request(self, method, path, **kwargs):
        url = self.base_url + path
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.exceptions.RequestException as ex:
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise CloudflareAPIError("{} {} failed: {}".format(method, path, ex)) from ex
            if response.status_code in self.RETRY_STATUSES and attempt < self.max_retries:
                time.sleep(2 ** attempt)
                continue
            try:
                body = response.json()
            except ValueError:
                body = {}
            if not response.ok or not body.get("success", False):
                errors = body.get("errors") or response.text[:300]
                raise CloudflareAPIError("HTTP {}: {}".format(response.status_code, errors))
            return body.get("result")

    def list_records(self, zone_id, name):
        return self._request("GET", "/zones/{}/dns_records".format(zone_id),
                             params={"name.exact": name, "per_page": 100}) or []

    def create_record(self, zone_id, data):
        return self._request("POST", "/zones/{}/dns_records".format(zone_id), json=data)

    def update_record(self, zone_id, record_id, data):
        return self._request("PUT", "/zones/{}/dns_records/{}".format(zone_id, record_id), json=data)


def env_bool(name, default="FALSE", env=None):
    env = os.environ if env is None else env
    return str(env.get(name, default)).strip().lower() in ("true", "1", "yes", "on")


def read_secret(name):
    """Resolve a setting from NAME_FILE, a Docker secret (NAME or name) or the environment."""
    file_path = os.environ.get(name + "_FILE")
    candidates = [file_path] if file_path else [
        os.path.join(SECRETS_DIR, name),
        os.path.join(SECRETS_DIR, name.lower()),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                value = handle.read().strip()
            if value:
                logger.debug("Read %s from secret file %s", name, path)
                return value
    if file_path:
        logger.error("%s_FILE is set to '%s' but that file is missing or empty", name, file_path)
    return os.environ.get(name) or None


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.dry_run = env_bool("DRY_RUN", env=env)
        self.refresh_entries = env_bool("REFRESH_ENTRIES", env=env)
        self.enable_docker_poll = env_bool("ENABLE_DOCKER_POLL", "TRUE", env)
        self.swarm_mode = env_bool("DOCKER_SWARM_MODE", env=env) or env_bool("SWARM_MODE", env=env)
        self.enable_traefik_poll = env_bool("ENABLE_TRAEFIK_POLL", env=env)
        self.traefik_poll_url = env.get("TRAEFIK_POLL_URL") or None
        self.traefik_poll_seconds = int(env.get("TRAEFIK_POLL_SECONDS", "60"))
        self.traefik_poll_timeout = int(env.get("TRAEFIK_POLL_TIMEOUT", "10"))
        version = env.get("TRAEFIK_VERSION", "2").strip()
        # Traefik v3 uses the same label and API format as v2
        self.traefik_version = "2" if version in ("2", "3") else version
        self.traefik_filter = env.get("TRAEFIK_FILTER")
        self.traefik_filter_label = env.get("TRAEFIK_FILTER_LABEL", "traefik.constraint")
        self.rc_type = env.get("RC_TYPE", "CNAME").upper()
        self.default_ttl = int(env.get("DEFAULT_TTL", "1"))


def setup_logging():
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_type = os.environ.get("LOG_TYPE", "BOTH").upper()
    log_file = os.path.join(os.environ.get("LOG_PATH", "/logs"), os.environ.get("LOG_FILE", "tcc.log"))

    fmt = "%(asctime)s %(levelname)s | %(message)s"
    if level == "DEBUG":
        fmt = "%(asctime)s %(levelname)s %(lineno)d | %(message)s"
    levels = {"DEBUG": logging.DEBUG, "VERBOSE": logging.DEBUG, "INFO": logging.INFO,
              "NOTICE": logging.INFO, "WARNING": logging.WARNING, "WARN": logging.WARNING,
              "ERROR": logging.ERROR}
    logger.setLevel(levels.get(level, logging.INFO))
    formatter = logging.Formatter(fmt, "%Y-%m-%dT%H:%M:%S%z")
    logger.handlers.clear()

    if log_type in ("CONSOLE", "BOTH"):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    if log_type in ("FILE", "BOTH"):
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=3)
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        except OSError as ex:
            if not logger.handlers:
                handler = logging.StreamHandler(sys.stdout)
                handler.setFormatter(formatter)
                logger.addHandler(handler)
            logger.warning("Can't write log file %s (%s), logging to console only", log_file, ex)

    if level not in levels:
        logger.warning("Unknown LOG_LEVEL '%s', using INFO", level)


def init_doms_from_env(target_domain, default_ttl, env=None):
    env = os.environ if env is None else env
    doms = []
    for key in sorted(env):
        if not RX_DOMAIN_VAR.match(key):
            continue
        zone_id = env.get("{}_ZONE_ID".format(key))
        if not zone_id:
            logger.error("*** ERROR: %s_ZONE_ID is not set, skipping %s", key, env[key])
            continue
        doms.append({
            "name": env[key].strip().lower().rstrip("."),
            "proxied": env.get("{}_PROXIED".format(key), "FALSE").strip().upper() == "TRUE",
            "zone_id": zone_id,
            "ttl": int(env.get("{}_TTL".format(key), default_ttl)),
            "target_domain": env.get("{}_TARGET_DOMAIN".format(key), target_domain),
            "comment": env.get("{}_COMMENT".format(key)) or None,
            "excluded_sub_domains": [s.strip().lower() for s in env.get("{}_EXCLUDED_SUB_DOMAINS".format(key), "").split(",") if s.strip()],
        })
    for dom in doms:
        logger.debug("Domain Configuration: %s", dom)
    return doms


def init_traefik_from_env(env=None):
    env = os.environ if env is None else env
    rx_included = re.compile(r"^TRAEFIK_INCLUDED_HOST[0-9]+$", re.IGNORECASE)
    rx_excluded = re.compile(r"^TRAEFIK_EXCLUDED_HOST[0-9]+$", re.IGNORECASE)
    included = [re.compile(env[k]) for k in sorted(env) if rx_included.match(k)]
    excluded = [re.compile(env[k]) for k in sorted(env) if rx_excluded.match(k)]
    if not included:
        included.append(re.compile(".*"))
    logger.debug("Traefik Host Includes: %s", [r.pattern for r in included])
    logger.debug("Traefik Host Excludes: %s", [r.pattern for r in excluded])
    return included, excluded


def extract_hosts_v1(rule):
    """Hosts from a Traefik v1 rule such as 'Host:a.example.com,b.example.com;PathPrefix:/x'."""
    hosts = []
    for part in rule.split(";"):
        part = part.strip()
        if part.startswith("Host:"):
            hosts.extend(h.strip() for h in part[len("Host:"):].split(",") if h.strip())
    return [h for h in hosts if RX_HOSTNAME.match(h)]


def extract_hosts_v2(rule):
    """Hosts from Host(`a`) matchers in a Traefik v2/v3 rule. HostRegexp/HostSNI are ignored."""
    hosts = []
    for matcher in RX_V2_HOST_MATCHER.finditer(rule):
        for host in RX_V2_BACKTICK.findall(matcher.group(1)):
            host = host.strip()
            if RX_HOSTNAME.match(host) and host not in hosts:
                hosts.append(host)
    return hosts


def domain_matches(name, dom):
    name = name.lower().rstrip(".")
    return name == dom["name"] or name.endswith("." + dom["name"])


def is_domain_excluded(name, dom):
    name = name.lower().rstrip(".")
    for sub_dom in dom["excluded_sub_domains"]:
        fqdn = sub_dom + "." + dom["name"]
        if name == fqdn or name.endswith("." + fqdn):
            logger.info("Ignoring %s because it falls under excluded sub domain: %s", name, sub_dom)
            return True
    return False


def is_matching(host, regexes):
    return any(regex.search(host) for regex in regexes)


def passes_filter(labels, config):
    """True when no TRAEFIK_FILTER is set, or a label matching TRAEFIK_FILTER_LABEL has a matching value."""
    if config.traefik_filter is None:
        return True
    for label, value in labels.items():
        if re.match(config.traefik_filter_label, label) and re.match(config.traefik_filter, value or ""):
            return True
    return False


def hosts_from_labels(labels, config):
    labels = labels or {}
    if not passes_filter(labels, config):
        return []
    hosts = []
    for label, value in labels.items():
        if not value:
            continue
        if config.traefik_version == "1" and RX_V1_RULE_LABEL.match(label):
            found = extract_hosts_v1(value)
        elif config.traefik_version == "2" and RX_V2_RULE_LABEL.match(label):
            found = extract_hosts_v2(value)
        else:
            continue
        hosts.extend(h for h in found if h not in hosts)
    return hosts


def hosts_from_routers(routers, included_hosts, excluded_hosts):
    hosts = []
    for router in routers:
        if not isinstance(router, dict) or router.get("status") != "enabled":
            continue
        name, rule = router.get("name"), router.get("rule")
        if not name or not rule:
            continue
        for host in extract_hosts_v2(rule):
            if not is_matching(host, included_hosts):
                logger.debug("Traefik Router %s host %s: no include match", name, host)
            elif is_matching(host, excluded_hosts):
                logger.debug("Traefik Router %s host %s: matched exclude", name, host)
            elif host not in hosts:
                logger.info("Found Traefik Router: %s with Hostname %s", name, host)
                hosts.append(host)
    return hosts


def add_to_mappings(current_mappings, hosts, source):
    for host in hosts:
        current = current_mappings.get(host)
        if current is None or current > source:
            current_mappings[host] = source


class Companion:
    def __init__(self, config, cf, doms, included_hosts, excluded_hosts, docker_client=None):
        self.config = config
        self.cf = cf
        self.doms = doms
        self.included_hosts = included_hosts
        self.excluded_hosts = excluded_hosts
        self.docker = docker_client
        self.synced_mappings = {}
        self.lock = threading.Lock()

    # Cloudflare

    def point_domain(self, name):
        ok = True
        for dom in self.doms:
            if name == dom["target_domain"] or not domain_matches(name, dom) or is_domain_excluded(name, dom):
                continue
            data = {
                "type": self.config.rc_type,
                "name": name,
                "content": dom["target_domain"],
                "ttl": int(dom["ttl"]),
                "proxied": bool(dom["proxied"]),
            }
            if dom["comment"]:
                data["comment"] = dom["comment"]
            try:
                records = self.cf.list_records(dom["zone_id"], name)
                if not records:
                    if self.config.dry_run:
                        logger.info("DRY-RUN: POST to Cloudflare %s: %s", dom["zone_id"], data)
                    else:
                        self.cf.create_record(dom["zone_id"], data)
                        logger.info("Created new record: %s to point to %s", name, dom["target_domain"])
                    continue
                for record in records:
                    if record.get("content") != dom["target_domain"] or self.config.refresh_entries:
                        if self.config.dry_run:
                            logger.info("DRY-RUN: PUT to Cloudflare %s, %s: %s", dom["zone_id"], record["id"], data)
                        else:
                            self.cf.update_record(dom["zone_id"], record["id"], data)
                            logger.info("Updated existing record: %s to point to %s", name, dom["target_domain"])
                    else:
                        logger.info("Existing record: %s already points to %s", name, dom["target_domain"])
            except CloudflareAPIError as ex:
                logger.error("Cloudflare API error for %s: %s", name, ex)
                ok = False
            except Exception:
                logger.exception("Unexpected error while updating %s", name)
                ok = False
        return ok

    def sync_mappings(self, mappings):
        with self.lock:
            for host, source in mappings.items():
                current = self.synced_mappings.get(host)
                if current is None or current > source:
                    if self.point_domain(host):
                        self.synced_mappings[host] = source

    # Docker

    def check_container(self, container):
        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        hosts = hosts_from_labels(labels, self.config)
        for host in hosts:
            logger.info("Found Container: %s with Hostname %s", container.name, host)
        return hosts

    def check_service(self, service):
        spec = service.attrs.get("Spec") or {}
        if self.config.traefik_version == "1":
            labels = ((spec.get("TaskTemplate") or {}).get("ContainerSpec") or {}).get("Labels") or {}
        else:
            labels = spec.get("Labels") or {}
        hosts = hosts_from_labels(labels, self.config)
        for host in hosts:
            logger.info("Found Service: %s with Hostname %s", spec.get("Name", service.id), host)
        return hosts

    def get_docker_mappings(self):
        mappings = {}
        for container in self.docker.containers.list():
            add_to_mappings(mappings, self.check_container(container), SOURCE_DOCKER)
        if self.config.swarm_mode:
            for service in self.docker.services.list():
                add_to_mappings(mappings, self.check_service(service), SOURCE_DOCKER)
        return mappings

    def handle_event(self, event):
        event_type = event.get("Type")
        action = event.get("Action") or event.get("status")
        actor_id = (event.get("Actor") or {}).get("ID") or event.get("id")
        mappings = {}
        try:
            if event_type == "container" and action == "start":
                add_to_mappings(mappings, self.check_container(self.docker.containers.get(actor_id)), SOURCE_DOCKER)
            elif event_type == "service" and action in ("create", "update") and self.config.swarm_mode:
                add_to_mappings(mappings, self.check_service(self.docker.services.get(actor_id)), SOURCE_DOCKER)
        except docker.errors.NotFound:
            logger.debug("%s %s disappeared before it could be inspected", event_type, actor_id)
            return
        if mappings:
            self.sync_mappings(mappings)

    def watch_docker_events(self, stop):
        types = ["container", "service"] if self.config.swarm_mode else ["container"]
        filters = {"type": types, "event": ["start", "create", "update"]}
        since = int(time.time())
        while not stop.is_set():
            try:
                logger.debug("Watching Docker events since %s", since)
                for event in self.docker.events(since=since, filters=filters, decode=True):
                    since = int(event.get("time", since))
                    self.handle_event(event)
            except (docker.errors.APIError, requests.exceptions.RequestException) as ex:
                logger.warning("Docker event stream interrupted (%s), reconnecting in 5s", ex)
            if not stop.wait(5):
                # Catch anything started while we were disconnected
                self.sync_mappings(self.get_docker_mappings())

    # Traefik

    def get_traefik_mappings(self):
        mappings = {}
        url = "{}/api/http/routers".format(self.config.traefik_poll_url.rstrip("/"))
        try:
            response = requests.get(url, timeout=self.config.traefik_poll_timeout)
        except requests.exceptions.RequestException as ex:
            logger.error("Traefik API request to %s failed: %s", url, ex)
            return mappings
        if not response.ok:
            logger.error("Traefik API returned HTTP %s: %s", response.status_code, response.text[:500])
            return mappings
        try:
            routers = response.json()
        except ValueError:
            logger.error("Traefik API returned invalid JSON: %s", response.text[:500])
            return mappings
        if not isinstance(routers, list):
            logger.error("Traefik API returned unexpected data: %s", str(routers)[:500])
            return mappings
        add_to_mappings(mappings, hosts_from_routers(routers, self.included_hosts, self.excluded_hosts), SOURCE_TRAEFIK)
        return mappings

    def poll_traefik(self, stop):
        while not stop.wait(self.config.traefik_poll_seconds):
            try:
                self.sync_mappings(self.get_traefik_mappings())
            except Exception:
                logger.exception("Traefik poll failed")


def uri_valid(value):
    try:
        result = urlparse(value)
        return bool(result.scheme and result.netloc)
    except (ValueError, AttributeError):
        return False


def main():
    setup_logging()
    logger.info("Starting Traefik Cloudflare Companion %s", __version__)
    config = Config()

    email = read_secret("CF_EMAIL")
    token = read_secret("CF_TOKEN")
    # Zone IDs may also come from Docker secrets (DOMAIN1_ZONE_ID_FILE etc.)
    for key in list(os.environ):
        if RX_DOMAIN_VAR.match(key):
            zone_id = read_secret("{}_ZONE_ID".format(key))
            if zone_id:
                os.environ["{}_ZONE_ID".format(key)] = zone_id
    target_domain = os.environ.get("TARGET_DOMAIN")
    if not target_domain:
        sys.exit("ERROR: TARGET_DOMAIN not defined")
    if not os.environ.get("DOMAIN1"):
        sys.exit("ERROR: DOMAIN1 not defined")
    if not token:
        sys.exit("ERROR: CF_TOKEN not defined")
    if config.swarm_mode and not config.enable_docker_poll:
        sys.exit("ERROR: Cannot enable DOCKER_SWARM_MODE without enabling ENABLE_DOCKER_POLL=true")

    if config.enable_traefik_poll:
        if config.traefik_version != "2":
            config.enable_traefik_poll = False
            logger.error("Traefik Polling Mode disabled because TRAEFIK_VERSION is not 2 or 3")
        elif not uri_valid(config.traefik_poll_url):
            config.enable_traefik_poll = False
            logger.error("Traefik Polling Mode disabled because TRAEFIK_POLL_URL is invalid: %s", config.traefik_poll_url)

    if config.dry_run:
        logger.warning("Dry Run: no changes will be made in Cloudflare")
    logger.debug("Docker Polling: %s", config.enable_docker_poll)
    logger.debug("Swarm Mode: %s", config.swarm_mode)
    logger.debug("Traefik Polling: %s", config.enable_traefik_poll)
    logger.debug("Refresh Entries: %s", config.refresh_entries)
    logger.debug("Traefik Version: %s", config.traefik_version)
    logger.debug("Default TTL: %s", config.default_ttl)

    if email:
        logger.debug("API Mode: Global")
        cf = CloudflareClient(token, email=email)
    else:
        logger.debug("API Mode: Scoped")
        cf = CloudflareClient(token)

    docker_client = None
    if config.enable_docker_poll:
        try:
            docker_client = docker.from_env(version="auto")
        except docker.errors.DockerException as ex:
            sys.exit("ERROR: can't connect to Docker at {} ({}). Mount the socket, run as root, "
                     "or point DOCKER_HOST at a socket proxy".format(os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock"), ex))
    doms = init_doms_from_env(target_domain, config.default_ttl)
    if not doms:
        sys.exit("ERROR: no usable DOMAIN<n> / DOMAIN<n>_ZONE_ID pairs configured")
    included_hosts, excluded_hosts = init_traefik_from_env()
    companion = Companion(config, cf, doms, included_hosts, excluded_hosts, docker_client)

    stop = threading.Event()

    def shutdown(signum, _frame):
        logger.info("Received signal %s, shutting down", signum)
        stop.set()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    initial = {}
    if config.enable_docker_poll:
        add_to_mappings(initial, companion.get_docker_mappings(), SOURCE_DOCKER)
    if config.enable_traefik_poll:
        for host, source in companion.get_traefik_mappings().items():
            add_to_mappings(initial, [host], source)
    companion.sync_mappings(initial)

    if config.enable_traefik_poll:
        logger.debug("Starting Traefik router polling every %ss", config.traefik_poll_seconds)
        threading.Thread(target=companion.poll_traefik, args=(stop,), daemon=True, name="traefik-poll").start()

    if config.enable_docker_poll:
        companion.watch_docker_events(stop)
    else:
        while not stop.wait(3600):
            pass


if __name__ == "__main__":
    main()
