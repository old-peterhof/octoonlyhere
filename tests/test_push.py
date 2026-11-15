"""Tests for the push plumbing.

The delivery test runs a local HTTP server standing in for a browser push
service, then decrypts what arrives with a subscriber key pair, the same way
a phone would. That covers VAPID signing and RFC 8291 payload encryption
without reaching the internet.
"""

import base64
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import http_ece
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from octoprint_printpocket.push import (
    ALL_EVENTS,
    PushSender,
    SubscriptionStore,
    VapidKeys,
)


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakePushService:
    """Collects push requests; replies with a configurable status code."""

    def __init__(self, status=201):
        self.requests = []
        self.status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(outer.status)
                self.end_headers()

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def service():
    s = FakePushService()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


def verify_es256_jwt(token, public_key):
    """Check a compact ES256 JWT signature and return its claims."""
    header, claims, sig = token.split(".")
    raw = b64url_decode(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public_key.verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert json.loads(b64url_decode(header))["alg"] == "ES256"
    return json.loads(b64url_decode(claims))


def make_subscriber(endpoint):
    """A browser-side key pair plus the subscription JSON it would hand us."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = b"0123456789abcdef"
    sub = {"endpoint": endpoint, "keys": {"p256dh": b64url(pub), "auth": b64url(auth)}}
    return priv, auth, sub


def test_vapid_keys_are_created_once_and_reused(tmp_path):
    a = VapidKeys(str(tmp_path)).ensure()
    raw = b64url_decode(a.public_key)
    assert len(raw) == 65 and raw[0] == 4  # uncompressed P-256 point

    b = VapidKeys(str(tmp_path)).ensure()
    assert b.public_key == a.public_key
    assert (tmp_path / "vapid_private.pem").stat().st_mode & 0o077 == 0


def test_store_upsert_filters_events_and_persists(tmp_path):
    store = SubscriptionStore(str(tmp_path))
    _, _, sub = make_subscriber("https://push.example/a")
    store.upsert(sub, ["done", "nonsense", "progress"], "iPhone", "admin")
    store.upsert(sub, ["failed"], "iPhone", "admin")  # same endpoint replaces

    again = SubscriptionStore(str(tmp_path))
    subs = again.all()
    assert len(subs) == 1
    assert subs[0]["events"] == ["failed"]
    assert subs[0]["user"] == "admin"

    assert again.remove("https://push.example/a") is True
    assert again.remove("https://push.example/a") is False
    assert SubscriptionStore(str(tmp_path)).all() == []


def test_corrupt_store_file_is_ignored(tmp_path):
    (tmp_path / "subscriptions.json").write_text("{not json")
    assert SubscriptionStore(str(tmp_path)).all() == []


def test_push_is_signed_encrypted_and_routed_by_event(tmp_path, service):
    keys = VapidKeys(str(tmp_path)).ensure()
    store = SubscriptionStore(str(tmp_path))

    done_priv, done_auth, done_sub = make_subscriber(service.url + "/done-phone")
    _, _, quiet_sub = make_subscriber(service.url + "/quiet-phone")
    store.upsert(done_sub, ["done"], "iPhone", "admin")
    store.upsert(quiet_sub, ["failed"], "Android", "admin")

    sender = PushSender(keys, store, "mailto:test@example.com", logging.getLogger("t"))
    payload = {"title": "Ender 3: done", "body": "benchy finished in 1h 02m"}
    assert sender.send("done", payload, background=False) == 1

    assert len(service.requests) == 1
    path, headers, body = service.requests[0]
    assert path == "/done-phone"
    assert headers["content-encoding"] == "aes128gcm"
    assert int(headers["ttl"]) > 0

    # VAPID: "vapid t=<jwt>, k=<public key>" signed by our key
    auth = headers["authorization"]
    assert auth.startswith("vapid ")
    parts = dict(p.strip().split("=", 1) for p in auth[len("vapid "):].split(","))
    assert parts["k"] == keys.public_key
    pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b64url_decode(parts["k"])
    )
    claims = verify_es256_jwt(parts["t"], pub)
    assert claims["aud"] == service.url
    assert claims["sub"] == "mailto:test@example.com"

    # The phone can decrypt it
    plain = http_ece.decrypt(body, private_key=done_priv, auth_secret=done_auth, version="aes128gcm")
    assert json.loads(plain) == payload


def test_only_endpoint_targets_one_device(tmp_path, service):
    keys = VapidKeys(str(tmp_path)).ensure()
    store = SubscriptionStore(str(tmp_path))
    _, _, a = make_subscriber(service.url + "/a")
    _, _, b = make_subscriber(service.url + "/b")
    store.upsert(a, [], "A", "u")
    store.upsert(b, [], "B", "u")
    sender = PushSender(keys, store, "mailto:x@example.com", logging.getLogger("t"))
    assert sender.send("test", {"title": "t"}, only_endpoint=b["endpoint"], background=False) == 1
    assert [r[0] for r in service.requests] == ["/b"]


@pytest.mark.parametrize("status,kept", [(410, False), (404, False), (500, True)])
def test_gone_subscriptions_are_removed(tmp_path, status, kept):
    service = FakePushService(status=status)
    try:
        keys = VapidKeys(str(tmp_path)).ensure()
        store = SubscriptionStore(str(tmp_path))
        _, _, sub = make_subscriber(service.url + "/old-phone")
        store.upsert(sub, ["done"], "iPhone", "admin")
        sender = PushSender(keys, store, "mailto:x@example.com", logging.getLogger("t"))
        sender.send("done", {"title": "x"}, background=False)
        assert (store.get(sub["endpoint"]) is not None) == kept
    finally:
        service.close()


def test_event_list_is_stable():
    # The web app renders toggles from this list; keep the order deliberate.
    assert ALL_EVENTS[:3] == ["started", "done", "failed"]
