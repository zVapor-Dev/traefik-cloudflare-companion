import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import cloudflare_companion as tcc  # noqa: E402


def dom(name="example.com", excluded=None):
    return {"name": name, "excluded_sub_domains": excluded or []}


@pytest.mark.parametrize("rule,expected", [
    ("Host(`a.example.com`)", ["a.example.com"]),
    ("Host(`a.example.com`) || Host(`b.example.com`)", ["a.example.com", "b.example.com"]),
    ("Host(`a.example.com`, `b.example.com`)", ["a.example.com", "b.example.com"]),
    ("Host(`a.example.com`) && PathPrefix(`/api`)", ["a.example.com"]),
    ("HostRegexp(`{sub:[a-z]+}.example.com`)", []),
    ("HostSNI(`*`)", []),
    ("Host(`a.example.com`) && Header(`X-Test`, `value`)", ["a.example.com"]),
    ("Host(`a.example.com`) || Host(`a.example.com`)", ["a.example.com"]),
])
def test_extract_hosts_v2(rule, expected):
    assert tcc.extract_hosts_v2(rule) == expected


@pytest.mark.parametrize("rule,expected", [
    ("Host:a.example.com", ["a.example.com"]),
    ("Host:a.example.com,b.example.com", ["a.example.com", "b.example.com"]),
    ("Host:a.example.com;PathPrefix:/x", ["a.example.com"]),
])
def test_extract_hosts_v1(rule, expected):
    assert tcc.extract_hosts_v1(rule) == expected


def test_domain_matches_is_suffix_not_substring():
    assert tcc.domain_matches("example.com", dom())
    assert tcc.domain_matches("a.example.com", dom())
    assert tcc.domain_matches("A.Example.com.", dom())
    assert not tcc.domain_matches("notexample.com", dom())
    assert not tcc.domain_matches("example.com.evil.net", dom())


def test_excluded_sub_domains():
    d = dom(excluded=["int"])
    assert tcc.is_domain_excluded("int.example.com", d)
    assert tcc.is_domain_excluded("app.int.example.com", d)
    assert not tcc.is_domain_excluded("print.example.com", d)
    assert not tcc.is_domain_excluded("app.example.com", d)


def make_config(**env):
    return tcc.Config(env={k: str(v) for k, v in env.items()})


def test_hosts_from_labels_v2():
    labels = {
        "traefik.enable": "true",
        "traefik.http.routers.app.rule": "Host(`app.example.com`)",
        "traefik.http.routers.app-secure.rule": "Host(`app.example.com`) || Host(`www.example.com`)",
        "traefik.http.services.app.loadbalancer.server.port": "80",
    }
    assert tcc.hosts_from_labels(labels, make_config()) == ["app.example.com", "www.example.com"]


def test_hosts_from_labels_handles_none():
    assert tcc.hosts_from_labels(None, make_config()) == []


def test_traefik_v3_is_treated_as_v2():
    assert make_config(TRAEFIK_VERSION="3").traefik_version == "2"


def test_filter_label():
    config = make_config(TRAEFIK_FILTER="proxy-public", TRAEFIK_FILTER_LABEL="traefik.constraint")
    public = {"traefik.constraint": "proxy-public", "traefik.http.routers.a.rule": "Host(`a.example.com`)"}
    private = {"traefik.constraint": "proxy-private", "traefik.http.routers.b.rule": "Host(`b.example.com`)"}
    unlabeled = {"traefik.http.routers.c.rule": "Host(`c.example.com`)"}
    assert tcc.hosts_from_labels(public, config) == ["a.example.com"]
    assert tcc.hosts_from_labels(private, config) == []
    assert tcc.hosts_from_labels(unlabeled, config) == []


def test_hosts_from_routers_filters():
    routers = [
        {"name": "a@docker", "status": "enabled", "rule": "Host(`a.example.com`)"},
        {"name": "b@file", "status": "disabled", "rule": "Host(`b.example.com`)"},
        {"name": "c@file", "status": "enabled", "rule": "Host(`c.internal.example.com`)"},
        {"name": "d@file", "status": "enabled"},
        "garbage",
    ]
    included = [tcc.re.compile(".*")]
    excluded = [tcc.re.compile(r"\.internal\.")]
    assert tcc.hosts_from_routers(routers, included, excluded) == ["a.example.com"]


def test_init_doms_from_env():
    env = {
        "DOMAIN1": "Example.com", "DOMAIN1_ZONE_ID": "z1", "DOMAIN1_PROXIED": "true",
        "DOMAIN1_EXCLUDED_SUB_DOMAINS": "int, lab",
        "DOMAIN2": "other.org",
    }
    doms = tcc.init_doms_from_env("target.example.com", 1, env=env)
    assert len(doms) == 1
    assert doms[0]["name"] == "example.com"
    assert doms[0]["proxied"] is True
    assert doms[0]["excluded_sub_domains"] == ["int", "lab"]
    assert doms[0]["comment"] is None


def test_read_secret_file(tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("abc123\n")
    monkeypatch.setenv("CF_TOKEN_FILE", str(secret))
    monkeypatch.setenv("CF_TOKEN", "from-env")
    assert tcc.read_secret("CF_TOKEN") == "abc123"


def test_read_secret_env_fallback(monkeypatch):
    monkeypatch.delenv("CF_TOKEN_FILE", raising=False)
    monkeypatch.setattr(tcc, "SECRETS_DIR", "/nonexistent")
    monkeypatch.setenv("CF_TOKEN", "from-env")
    assert tcc.read_secret("CF_TOKEN") == "from-env"


class FakeCF:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.created = []
        self.updated = []

    def list_records(self, zone_id, name):
        return [r for r in self.existing if r["name"] == name]

    def create_record(self, zone_id, data):
        self.created.append(data)

    def update_record(self, zone_id, record_id, data):
        self.updated.append((record_id, data))


def make_companion(records, **env):
    env.setdefault("DRY_RUN", "false")
    doms = [{"name": "example.com", "zone_id": "z1", "ttl": 1, "proxied": False,
             "target_domain": "target.example.com", "comment": None, "excluded_sub_domains": []}]
    return tcc.Companion(make_config(**env), records, doms, [], [])


def test_point_domain_creates_without_null_comment():
    records = FakeCF()
    assert make_companion(records).point_domain("app.example.com")
    assert records.created == [{"type": "CNAME", "name": "app.example.com", "content": "target.example.com",
                                "ttl": 1, "proxied": False}]


def test_point_domain_skips_other_zones_and_target():
    records = FakeCF()
    companion = make_companion(records)
    companion.point_domain("app.notexample.com")
    companion.point_domain("target.example.com")
    assert records.created == []


def test_point_domain_updates_changed_record():
    existing = {"id": "r1", "name": "app.example.com", "content": "old.example.com"}
    records = FakeCF([existing])
    make_companion(records).point_domain("app.example.com")
    assert records.updated[0][0] == "r1"


def test_point_domain_dry_run_does_not_write():
    existing = {"id": "r1", "name": "app.example.com", "content": "old.example.com"}
    records = FakeCF([existing])
    make_companion(records, DRY_RUN="true").point_domain("app.example.com")
    make_companion(FakeCF(), DRY_RUN="true").point_domain("new.example.com")
    assert records.updated == [] and records.created == []


class FakeResponse:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self.ok = status < 400
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("Expecting value: line 6 column 1")
        return self._body


@pytest.mark.parametrize("response", [
    FakeResponse(200, None, "<html>error</html>"),  # issue #126 upstream: non-JSON body crashed the app
    FakeResponse(500, None, "internal error"),
    FakeResponse(200, {"unexpected": "object"}),
])
def test_traefik_poll_bad_responses_do_not_crash(monkeypatch, response):
    monkeypatch.setattr(tcc.requests, "get", lambda url, timeout: response)
    companion = make_companion(FakeCF(), TRAEFIK_POLL_URL="http://traefik:8080")
    companion.included_hosts = [tcc.re.compile(".*")]
    assert companion.get_traefik_mappings() == {}


def test_traefik_poll_success(monkeypatch):
    routers = [{"name": "a@file", "status": "enabled", "rule": "Host(`a.example.com`)"}]
    monkeypatch.setattr(tcc.requests, "get", lambda url, timeout: FakeResponse(200, routers))
    companion = make_companion(FakeCF(), TRAEFIK_POLL_URL="http://traefik:8080/")
    companion.included_hosts = [tcc.re.compile(".*")]
    assert companion.get_traefik_mappings() == {"a.example.com": tcc.SOURCE_TRAEFIK}
