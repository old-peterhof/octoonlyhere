"""PrintPocket: a phone-first web app and push notifications for OctoPrint.

The app lives at http://<your-pi>/plugin/printpocket/ and talks to OctoPrint's
own REST API with an application key. This plugin adds:

* the installable web app (PWA) itself
* Web Push notifications for print events, sent straight from the Pi
* a webcam snapshot endpoint that works even when the snapshot URL is only
  reachable from the Pi (the OctoPi default is http://127.0.0.1:8080/...)
"""

import functools
import os
import socket
import time

import flask
import octoprint.plugin
from flask_login import current_user
from octoprint.access.permissions import Permissions
from octoprint.events import Events

from .push import ALL_EVENTS, DEFAULT_EVENTS, PushSender, SubscriptionStore, VapidKeys

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "app")


def _require(permission):
    """Gate a view on an OctoPrint permission.

    The blueprint is unprotected so the app shell, manifest and service worker
    load before the user has a key. Every data endpoint goes through here.
    """

    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user or current_user.is_anonymous:
                flask.abort(401)
            if not current_user.has_permission(permission):
                flask.abort(403)
            return f(*args, **kwargs)

        return wrapper

    return decorator


def _fmt_duration(seconds):
    if seconds is None:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m"
    return f"{seconds}s"


def _display_name(payload):
    name = (payload or {}).get("name") or (payload or {}).get("path") or "print"
    for ext in (".gcode", ".gco", ".g", ".bgcode"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


class PrintPocketPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.BlueprintPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.ProgressPlugin,
):
    def __init__(self):
        self._keys = None
        self._store = None
        self._sender = None
        self._last_progress_step = -1

    # ~~ lifecycle

    def initialize(self):
        # Runs before the server accepts requests, so the endpoints never see
        # a half set up plugin.
        folder = self.get_plugin_data_folder()
        self._keys = VapidKeys(folder)
        try:
            self._keys.ensure()
        except Exception:
            self._logger.exception("Could not create VAPID keys; push is unavailable")
            self._keys = None
        self._store = SubscriptionStore(folder)
        self._sender = PushSender(
            self._keys, self._store, self._settings.get(["vapid_subject"]), self._logger
        )

    def on_after_startup(self):
        self._logger.info(
            "PrintPocket ready at /plugin/printpocket/ with %d push subscriber(s)",
            len(self._store.all()),
        )

    # ~~ settings

    def get_settings_defaults(self):
        return {
            "progress_step": 25,
            "vapid_subject": "mailto:printpocket@users.noreply.github.com",
            "printer_name": "",
        }

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        if self._sender:
            self._sender.claims_sub = self._settings.get(["vapid_subject"])

    def is_template_autoescaped(self):
        return True

    def get_template_configs(self):
        return [{"type": "settings", "custom_bindings": False}]

    # ~~ helpers

    def _printer_name(self):
        return (
            self._settings.get(["printer_name"])
            or self._settings.global_get(["appearance", "name"])
            or socket.gethostname()
        )

    def _webcams(self):
        try:
            from octoprint.webcams import get_webcams

            return get_webcams()
        except Exception:
            self._logger.exception("Could not list webcams")
            return {}

    def _notify(self, event, title, body, tag=None):
        if not self._sender or not self._keys:
            return 0
        payload = {
            "title": title,
            "body": body,
            "tag": tag or event,
            "event": event,
            "printer": self._printer_name(),
            "ts": int(time.time()),
            "url": "/plugin/printpocket/",
        }
        return self._sender.send(event, payload)

    # ~~ events

    def on_event(self, event, payload):
        name = self._printer_name()
        file = _display_name(payload)

        if event == Events.PRINT_STARTED:
            self._last_progress_step = 0
            self._notify("started", f"{name}: printing", f"Started {file}", tag="job")
        elif event == Events.PRINT_DONE:
            took = _fmt_duration((payload or {}).get("time"))
            self._notify(
                "done", f"{name}: done", f"{file} finished in {took}".strip(), tag="job"
            )
        elif event == Events.PRINT_FAILED:
            reason = (payload or {}).get("reason")
            if reason == "cancelled":
                return  # PrintCancelled covers it
            self._notify("failed", f"{name}: print failed", f"{file} failed", tag="job")
        elif event == Events.PRINT_CANCELLED:
            self._notify(
                "cancelled", f"{name}: cancelled", f"{file} was cancelled", tag="job"
            )
        elif event == Events.PRINT_PAUSED:
            self._notify("paused", f"{name}: paused", f"{file} is paused", tag="job")
        elif event == getattr(Events, "FILAMENT_CHANGE", "FilamentChange"):
            self._notify(
                "filament",
                f"{name}: filament change",
                "The printer is waiting for a filament change",
                tag="job",
            )
        elif event == Events.ERROR:
            err = (payload or {}).get("error") or "Unknown error"
            self._notify("error", f"{name}: printer error", err, tag="error")

    def on_print_progress(self, storage, path, progress):
        step = int(self._settings.get_int(["progress_step"]) or 0)
        if step <= 0 or progress <= 0 or progress >= 100:
            return
        bucket = progress // step
        if bucket <= self._last_progress_step:
            return
        self._last_progress_step = bucket
        name = self._printer_name()
        file = _display_name({"path": path})
        self._notify(
            "progress", f"{name}: {progress}%", f"{file} is {progress}% done", tag="job"
        )

    # ~~ blueprint

    def is_blueprint_protected(self):
        return False

    def is_blueprint_csrf_protected(self):
        # Data endpoints are called with an X-Api-Key header, which OctoPrint
        # already exempts from CSRF checks.
        return True

    def get_blueprint_api_prefixes(self):
        return ["/api"]

    @octoprint.plugin.BlueprintPlugin.route("/", methods=["GET"])
    def app_index(self):
        return self._app_file("index.html", "text/html", no_cache=True)

    @octoprint.plugin.BlueprintPlugin.route("/sw.js", methods=["GET"])
    def app_sw(self):
        resp = self._app_file("sw.js", "application/javascript", no_cache=True)
        resp.headers["Service-Worker-Allowed"] = "/plugin/printpocket/"
        return resp

    @octoprint.plugin.BlueprintPlugin.route("/manifest.webmanifest", methods=["GET"])
    def app_manifest(self):
        name = self._printer_name()
        manifest = {
            "name": f"PrintPocket: {name}",
            "short_name": name[:12] or "PrintPocket",
            "id": "/plugin/printpocket/",
            "start_url": "/plugin/printpocket/",
            "scope": "/plugin/printpocket/",
            "display": "standalone",
            "background_color": "#0f1115",
            "theme_color": "#0f1115",
            "icons": [
                {
                    "src": "static/app/icons/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                },
                {
                    "src": "static/app/icons/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                },
                {
                    "src": "static/app/icons/icon-maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        }
        resp = flask.make_response(flask.json.dumps(manifest))
        resp.headers["Content-Type"] = "application/manifest+json"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    def _app_file(self, filename, mimetype, no_cache=False):
        resp = flask.make_response(flask.send_from_directory(APP_DIR, filename))
        resp.headers["Content-Type"] = mimetype
        if no_cache:
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @octoprint.plugin.BlueprintPlugin.route("/api/info", methods=["GET"])
    @_require(Permissions.STATUS)
    def api_info(self):
        webcams = []
        for name, wc in self._webcams().items():
            cfg = wc.config
            compat = cfg.compat
            webcams.append(
                {
                    "name": name,
                    "displayName": cfg.displayName,
                    "stream": compat.stream if compat else None,
                    "canSnapshot": bool(cfg.canSnapshot),
                    "flipH": cfg.flipH,
                    "flipV": cfg.flipV,
                    "rotate90": cfg.rotate90,
                    "ratio": getattr(compat.streamRatio, "value", "16:9")
                    if compat
                    else "16:9",
                }
            )

        profiles = [
            {
                "name": p.get("name"),
                "extruder": p.get("extruder", 0),
                "bed": p.get("bed", 0),
            }
            for p in (self._settings.global_get(["temperature", "profiles"]) or [])
        ]

        return flask.jsonify(
            printer=self._printer_name(),
            version=self._plugin_version,
            user=current_user.get_name(),
            vapidPublicKey=self._keys.public_key if self._keys else None,
            events={"all": ALL_EVENTS, "defaults": DEFAULT_EVENTS},
            progressStep=self._settings.get_int(["progress_step"]),
            webcams=webcams,
            temperatureProfiles=profiles,
            permissions={
                "control": current_user.has_permission(Permissions.CONTROL),
                "print": current_user.has_permission(Permissions.PRINT),
                "connection": current_user.has_permission(Permissions.CONNECTION),
            },
        )

    @octoprint.plugin.BlueprintPlugin.route("/api/snapshot", methods=["GET"])
    @_require(Permissions.WEBCAM)
    def api_snapshot(self):
        webcams = self._webcams()
        wanted = flask.request.args.get("webcam")
        cam = webcams.get(wanted) if wanted else None
        if cam is None:
            cam = next((w for w in webcams.values() if w.config.canSnapshot), None)
        if cam is None or not cam.config.canSnapshot:
            flask.abort(404, description="No webcam that can take snapshots")

        provider = self._plugin_manager.get_plugin_info(cam.providerIdentifier)
        if provider is None or provider.implementation is None:
            flask.abort(404, description="Webcam provider not loaded")

        try:
            chunks = provider.implementation.take_webcam_snapshot(cam.config.name)
            data = b"".join(chunks)
        except Exception as e:
            self._logger.warning("Snapshot from %s failed: %s", cam.config.name, e)
            flask.abort(502, description="Snapshot failed")

        resp = flask.make_response(data)
        resp.headers["Content-Type"] = "image/jpeg"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @octoprint.plugin.BlueprintPlugin.route("/api/push", methods=["GET"])
    @_require(Permissions.STATUS)
    def api_push_get(self):
        endpoint = flask.request.args.get("endpoint")
        sub = self._store.get(endpoint) if (endpoint and self._store) else None
        return flask.jsonify(
            subscribed=sub is not None,
            events=sub["events"] if sub else DEFAULT_EVENTS,
        )

    @octoprint.plugin.BlueprintPlugin.route("/api/push", methods=["POST"])
    @_require(Permissions.STATUS)
    def api_push_subscribe(self):
        data = flask.request.get_json(silent=True) or {}
        sub = data.get("subscription") or {}
        keys = sub.get("keys") or {}
        if not self._keys:
            flask.abort(503, description="Push keys are missing, check the OctoPrint log")
        if not (
            isinstance(sub.get("endpoint"), str)
            and sub["endpoint"].startswith("https://")
            and keys.get("p256dh")
            and keys.get("auth")
        ):
            flask.abort(400, description="Invalid push subscription")
        events = data.get("events")
        if not isinstance(events, list):
            events = DEFAULT_EVENTS
        entry = self._store.upsert(
            sub, events, data.get("device"), current_user.get_name()
        )
        return flask.jsonify(subscribed=True, events=entry["events"])

    # POST rather than DELETE: OctoPrint's server drops DELETE bodies.
    @octoprint.plugin.BlueprintPlugin.route("/api/push/unsubscribe", methods=["POST"])
    @_require(Permissions.STATUS)
    def api_push_unsubscribe(self):
        data = flask.request.get_json(silent=True) or {}
        endpoint = data.get("endpoint")
        if not endpoint:
            flask.abort(400, description="Missing endpoint")
        removed = self._store.remove(endpoint)
        return flask.jsonify(subscribed=False, removed=removed)

    @octoprint.plugin.BlueprintPlugin.route("/api/push/test", methods=["POST"])
    @_require(Permissions.STATUS)
    def api_push_test(self):
        data = flask.request.get_json(silent=True) or {}
        endpoint = data.get("endpoint")
        if not endpoint or not self._store.get(endpoint):
            flask.abort(404, description="This device is not subscribed")
        if not self._keys:
            flask.abort(503, description="Push keys are missing, check the OctoPrint log")
        name = self._printer_name()
        payload = {
            "title": f"{name}: test",
            "body": "Notifications from your printer are working.",
            "tag": "test",
            "event": "test",
            "printer": name,
            "ts": int(time.time()),
            "url": "/plugin/printpocket/",
        }
        self._sender.send("test", payload, only_endpoint=endpoint)
        return flask.jsonify(sent=True)

    # ~~ software update

    def get_update_information(self):
        return {
            "printpocket": {
                "displayName": "PrintPocket",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "CHANGE_ME",
                "repo": "octoprint-printpocket",
                "current": self._plugin_version,
                "pip": "https://github.com/CHANGE_ME/octoprint-printpocket/archive/{target_version}.zip",
            }
        }


__plugin_name__ = "PrintPocket"
__plugin_pythoncompat__ = ">=3.7,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = PrintPocketPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
    }
