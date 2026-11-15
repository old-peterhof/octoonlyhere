"""Web Push plumbing: VAPID keys, the subscription store, and delivery.

Everything here runs on the Pi. Push messages go straight from OctoPrint to
the push service of the phone's browser (Apple, Google or Mozilla). No relay
server of ours sits in between.
"""

import base64
import json
import logging
import os
import threading
import time

ALL_EVENTS = [
    "started",
    "done",
    "failed",
    "cancelled",
    "paused",
    "filament",
    "progress",
    "error",
]

DEFAULT_EVENTS = ["done", "failed", "paused", "filament", "error"]


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class VapidKeys:
    """Loads, or creates on first use, the VAPID key pair for this printer."""

    def __init__(self, data_folder):
        self.pem_path = os.path.join(data_folder, "vapid_private.pem")
        self._public_key = None

    def ensure(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        if os.path.exists(self.pem_path):
            with open(self.pem_path, "rb") as f:
                private_key = serialization.load_pem_private_key(f.read(), password=None)
        else:
            private_key = ec.generate_private_key(ec.SECP256R1())
            pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            tmp = self.pem_path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(pem)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.pem_path)

        raw = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        self._public_key = _b64url(raw)
        return self

    @property
    def public_key(self):
        if self._public_key is None:
            self.ensure()
        return self._public_key


class SubscriptionStore:
    """A small JSON file of push subscriptions, one per phone or browser."""

    def __init__(self, data_folder):
        self.path = os.path.join(data_folder, "subscriptions.json")
        self._lock = threading.RLock()
        self._subs = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [s for s in data if isinstance(s, dict) and s.get("endpoint")]
        except FileNotFoundError:
            pass
        except Exception:
            logging.getLogger(__name__).exception("Could not read %s", self.path)
        return []

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._subs, f, indent=2)
        os.replace(tmp, self.path)

    def all(self):
        with self._lock:
            return [dict(s) for s in self._subs]

    def get(self, endpoint):
        with self._lock:
            for s in self._subs:
                if s["endpoint"] == endpoint:
                    return dict(s)
        return None

    def upsert(self, subscription, events, device, user):
        events = [e for e in events if e in ALL_EVENTS]
        entry = {
            "endpoint": subscription["endpoint"],
            "keys": {
                "p256dh": subscription["keys"]["p256dh"],
                "auth": subscription["keys"]["auth"],
            },
            "events": events,
            "device": (device or "")[:80],
            "user": user,
            "updated": int(time.time()),
        }
        with self._lock:
            self._subs = [s for s in self._subs if s["endpoint"] != entry["endpoint"]]
            self._subs.append(entry)
            self._save()
        return entry

    def remove(self, endpoint):
        with self._lock:
            before = len(self._subs)
            self._subs = [s for s in self._subs if s["endpoint"] != endpoint]
            if len(self._subs) != before:
                self._save()
                return True
        return False


class PushSender:
    def __init__(self, keys, store, claims_sub, logger):
        self.keys = keys
        self.store = store
        self.claims_sub = claims_sub
        self._logger = logger

    def send(self, event, payload, only_endpoint=None, background=True):
        """Send payload to every subscription that opted into event.

        Returns the number of subscriptions that were attempted. Delivery itself
        happens on a background thread so print events never block.
        """
        targets = [
            s
            for s in self.store.all()
            if (only_endpoint is None and event in s.get("events", []))
            or (only_endpoint is not None and s["endpoint"] == only_endpoint)
        ]
        if not targets:
            return 0

        body = json.dumps(payload)
        if background:
            t = threading.Thread(
                target=self._deliver, args=(targets, body), name="printpocket-push"
            )
            t.daemon = True
            t.start()
        else:
            self._deliver(targets, body)
        return len(targets)

    def _deliver(self, targets, body):
        from pywebpush import WebPushException, webpush

        for sub in targets:
            try:
                webpush(
                    subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                    data=body,
                    vapid_private_key=self.keys.pem_path,
                    vapid_claims={"sub": self.claims_sub},
                    ttl=60 * 60 * 12,
                    timeout=15,
                )
            except WebPushException as e:
                status = getattr(e.response, "status_code", None)
                if status in (404, 410):
                    self._logger.info(
                        "Push subscription for %s is gone, removing it", sub.get("device")
                    )
                    self.store.remove(sub["endpoint"])
                else:
                    self._logger.warning(
                        "Push to %s failed (%s): %s", sub.get("device"), status, e
                    )
            except Exception:
                self._logger.exception("Push to %s failed", sub.get("device"))
