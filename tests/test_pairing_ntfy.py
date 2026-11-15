import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from octoprint_printpocket.ntfy import NtfySender, new_topic_url, valid_topic_url
from octoprint_printpocket.pairing import (
    ALPHABET,
    CODE_TTL,
    MAX_FAILURES,
    LockedOut,
    PairingCodes,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---------- pairing ----------


def test_code_shape_and_single_use():
    p = PairingCodes()
    code, ttl = p.create("admin")
    assert ttl == CODE_TTL
    assert len(code) == 6 and all(c in ALPHABET for c in code)
    assert p.claim(code) == "admin"
    assert p.claim(code) is None  # burned


def test_code_is_forgiving_about_case_and_spaces():
    p = PairingCodes()
    code, _ = p.create("admin")
    assert p.claim(f" {code[:3].lower()} - {code[3:]} ") == "admin"


def test_code_expires():
    clock = Clock()
    p = PairingCodes(clock=clock)
    code, _ = p.create("admin")
    clock.t += CODE_TTL + 1
    assert p.claim(code) is None


def test_new_code_replaces_old_one_for_same_user():
    p = PairingCodes()
    first, _ = p.create("admin")
    second, _ = p.create("admin")
    other, _ = p.create("guest")
    assert p.claim(first) is None or first == second
    assert p.claim(second) == "admin"
    assert p.claim(other) == "guest"


def test_guessing_is_capped():
    clock = Clock()
    p = PairingCodes(clock=clock)
    code, _ = p.create("admin")
    for _ in range(MAX_FAILURES):
        assert p.claim("ZZZZZZ" if code != "ZZZZZZ" else "YYYYYY") is None
    with pytest.raises(LockedOut):
        p.claim(code)
    clock.t += 11 * 60  # window passes, but the code has expired by now too
    assert p.claim(code) is None


# ---------- ntfy ----------


class FakeNtfy:
    def __init__(self, status=200, refuse_attachments=False):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                u = urlsplit(self.path)
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": u.path,
                        "query": {k: v[0] for k, v in parse_qs(u.query).items()},
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                code = status
                if refuse_attachments and self.command == "PUT":
                    code = 413
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = _handle
            do_PUT = _handle

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


def test_topic_urls():
    url = new_topic_url()
    assert url.startswith("https://ntfy.sh/printpocket-") and len(url) > 40
    assert new_topic_url() != url
    assert new_topic_url("https://ntfy.example.com/").startswith("https://ntfy.example.com/printpocket-")
    assert valid_topic_url(url)
    assert not valid_topic_url("")
    assert not valid_topic_url("https://ntfy.sh")
    assert not valid_topic_url("ftp://x/y")


def test_text_notification():
    srv = FakeNtfy()
    try:
        ok = NtfySender(logging.getLogger("t")).send(
            srv.url + "/my-topic", "failed", "Ender 3: print failed", "Bénchy failed",
            token="tk_123", click="http://octopi.netbird.cloud/plugin/printpocket/", background=False,
        )
        assert ok
        (r,) = srv.requests
        assert r["method"] == "POST" and r["path"] == "/my-topic"
        assert r["query"]["title"] == "Ender 3: print failed"
        assert r["query"]["message"] == "Bénchy failed"
        assert r["query"]["tags"] == "x"
        assert r["query"]["priority"] == "4"
        assert r["query"]["click"].endswith("/plugin/printpocket/")
        assert r["headers"]["authorization"] == "Bearer tk_123"
    finally:
        srv.close()


def test_snapshot_attachment_and_fallback():
    srv = FakeNtfy()
    try:
        NtfySender(logging.getLogger("t")).send(
            srv.url + "/t", "done", "done", "benchy", image=b"\xff\xd8jpeg", background=False
        )
        (r,) = srv.requests
        assert r["method"] == "PUT" and r["body"] == b"\xff\xd8jpeg"
        assert r["headers"]["filename"] == "snapshot.jpg"
    finally:
        srv.close()

    srv = FakeNtfy(refuse_attachments=True)
    try:
        assert NtfySender(logging.getLogger("t")).send(
            srv.url + "/t", "done", "done", "benchy", image=b"jpeg", background=False
        )
        assert [r["method"] for r in srv.requests] == ["PUT", "POST"]
    finally:
        srv.close()


def test_server_error_is_reported_not_raised():
    srv = FakeNtfy(status=500)
    try:
        assert NtfySender(logging.getLogger("t")).send(srv.url + "/t", "done", "a", "b", background=False) is False
    finally:
        srv.close()
    assert NtfySender(logging.getLogger("t")).send("not a url", "done", "a", "b", background=False) is False
