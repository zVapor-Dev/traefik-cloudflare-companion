"""End-to-end check of the Cloudflare calls against a local fake of the v4 API.

This checks the exact requests (query parameters, HTTP methods, auth headers,
no null fields) that unit tests with a fake client can't see.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import cloudflare_companion as tcc  # noqa: E402

ZONE = "023e105f4ecef8ad9ca31a8372d0c353"


class FakeCloudflare(BaseHTTPRequestHandler):
    records = []
    requests = []

    def log_message(self, *args):
        pass

    def _reply(self, result, status=200, info=False):
        body = {"success": status < 400, "errors": [], "messages": [], "result": result}
        if info:
            body["result_info"] = {"page": 1, "per_page": 100, "count": len(result),
                                   "total_count": len(result), "total_pages": 1}
        if status >= 400:
            body["errors"] = [{"code": 9109, "message": "Invalid access token"}]
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _record(self, data, record_id):
        return {"id": record_id, "zone_id": ZONE, "zone_name": "example.com", "proxiable": True,
                "created_on": "2026-10-22T00:00:00Z", "modified_on": "2026-10-22T00:00:00Z",
                "meta": {}, "tags": [], "settings": {}, **data}

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        type(self).requests.append(("GET", url.path, query))
        if self.headers.get("Authorization") != "Bearer good-token":
            return self._reply(None, 403)
        name = (query.get("name.exact") or query.get("name") or [None])[0]
        self._reply([r for r in type(self).records if r["name"] == name], info=True)

    def do_POST(self):
        data = self._body()
        type(self).requests.append(("POST", urlparse(self.path).path, data))
        record = self._record(data, "rec-%d" % (len(type(self).records) + 1))
        type(self).records.append(record)
        self._reply(record)

    def _update(self, method):
        data = self._body()
        path = urlparse(self.path).path
        type(self).requests.append((method, path, data))
        record_id = path.rstrip("/").rsplit("/", 1)[1]
        for record in type(self).records:
            if record["id"] == record_id:
                record.update(data)
                return self._reply(record)
        self._reply(None, 404)

    def do_PUT(self):
        self._update("PUT")

    def do_PATCH(self):
        self._update("PATCH")


@pytest.fixture
def cf_server():
    FakeCloudflare.records = []
    FakeCloudflare.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCloudflare)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def companion_for(server, token="good-token", **env):
    env.setdefault("DRY_RUN", "false")
    config = tcc.Config(env=env)
    cf = tcc.CloudflareClient(token, base_url="http://127.0.0.1:%d/client/v4" % server.server_port, max_retries=0)
    doms = tcc.init_doms_from_env("target.example.com", 1, env={
        "DOMAIN1": "example.com", "DOMAIN1_ZONE_ID": ZONE, **{k: v for k, v in env.items() if k.startswith("DOMAIN1_")}})
    return tcc.Companion(config, cf, doms, [], [])


def test_creates_record(cf_server):
    assert companion_for(cf_server).point_domain("app.example.com")
    method, path, query = FakeCloudflare.requests[0]
    assert (method, path) == ("GET", "/client/v4/zones/%s/dns_records" % ZONE)
    assert query.get("name.exact") == ["app.example.com"]
    method, path, body = FakeCloudflare.requests[1]
    assert method == "POST"
    assert body == {"type": "CNAME", "name": "app.example.com", "content": "target.example.com",
                    "ttl": 1, "proxied": False}


def test_sends_comment_and_proxied(cf_server):
    companion_for(cf_server, DOMAIN1_PROXIED="TRUE", DOMAIN1_COMMENT="managed by tcc").point_domain("app.example.com")
    body = FakeCloudflare.requests[1][2]
    assert body["proxied"] is True and body["comment"] == "managed by tcc"


def test_existing_record_is_left_alone(cf_server):
    companion = companion_for(cf_server)
    companion.point_domain("app.example.com")
    companion.point_domain("app.example.com")
    assert [r[0] for r in FakeCloudflare.requests] == ["GET", "POST", "GET"]


def test_changed_record_is_updated(cf_server):
    companion_for(cf_server).point_domain("app.example.com")
    FakeCloudflare.records[0]["content"] = "old.example.com"
    companion_for(cf_server).point_domain("app.example.com")
    method, path, body = FakeCloudflare.requests[-1]
    assert method == "PUT"
    assert path.endswith("/dns_records/rec-1")
    assert body["content"] == "target.example.com"
    assert FakeCloudflare.records[0]["content"] == "target.example.com"


def test_dry_run_only_reads(cf_server):
    companion_for(cf_server, DRY_RUN="true").point_domain("app.example.com")
    assert [r[0] for r in FakeCloudflare.requests] == ["GET"]


def test_bad_token_is_logged_not_raised(cf_server, caplog):
    assert companion_for(cf_server, token="bad-token").point_domain("app.example.com") is False
    assert "Cloudflare API error for app.example.com" in caplog.text


def test_global_api_key_headers(cf_server):
    client = tcc.CloudflareClient("key", email="me@example.com", base_url="http://127.0.0.1:%d/client/v4" % cf_server.server_port)
    assert client.session.headers["X-Auth-Email"] == "me@example.com"
    assert client.session.headers["X-Auth-Key"] == "key"
    assert "Authorization" not in client.session.headers


def test_retries_on_server_error(cf_server, monkeypatch):
    monkeypatch.setattr(tcc.time, "sleep", lambda s: None)
    calls = []
    original = FakeCloudflare.do_GET

    def flaky(self):
        calls.append(1)
        if len(calls) == 1:
            return self._reply(None, 503)
        return original(self)

    monkeypatch.setattr(FakeCloudflare, "do_GET", flaky)
    client = tcc.CloudflareClient("good-token", base_url="http://127.0.0.1:%d/client/v4" % cf_server.server_port)
    assert client.list_records(ZONE, "app.example.com") == []
    assert len(calls) == 2
