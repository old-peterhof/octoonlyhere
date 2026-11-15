"""Notifications through ntfy (https://ntfy.sh or a self-hosted ntfy server).

This path needs no HTTPS on the Pi, which makes it the easy option behind
NetBird, WireGuard or any VPN without automatic certificates. The phone runs
the free ntfy app and subscribes to a long random topic.
"""

import secrets
import threading
from urllib.parse import urlsplit

TAGS = {
    "started": "printer",
    "done": "white_check_mark",
    "failed": "x",
    "cancelled": "no_entry_sign",
    "paused": "pause_button",
    "filament": "yarn",
    "progress": "hourglass_flowing_sand",
    "error": "warning",
    "test": "bell",
}

PRIORITY = {"failed": 4, "error": 5, "filament": 4, "paused": 4, "progress": 2}


def new_topic_url(server="https://ntfy.sh"):
    """A topic name long enough that nobody guesses it."""
    return f"{server.rstrip('/')}/printpocket-{secrets.token_urlsafe(18).replace('_', 'x').replace('-', 'y')}"


def valid_topic_url(url):
    if not url:
        return False
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc) and len(parts.path.strip("/")) > 0


class NtfySender:
    def __init__(self, logger):
        self._logger = logger

    def send(self, url, event, title, message, token=None, click=None, image=None, background=True):
        if not valid_topic_url(url):
            return False
        args = (url, event, title, message, token, click, image)
        if background:
            t = threading.Thread(target=self._deliver, args=args, name="printpocket-ntfy")
            t.daemon = True
            t.start()
            return True
        return self._deliver(*args)

    def _deliver(self, url, event, title, message, token, click, image):
        import requests

        # Query parameters instead of headers so non-ASCII file names survive.
        params = {"title": title, "message": message, "tags": TAGS.get(event, "printer")}
        if event in PRIORITY:
            params["priority"] = str(PRIORITY[event])
        if click:
            params["click"] = click
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            if image:
                headers["Filename"] = "snapshot.jpg"
                r = requests.put(url, params=params, data=image, headers=headers, timeout=20)
                if r.status_code == 413 or r.status_code == 400:
                    # Server refuses attachments: send the text alone.
                    headers.pop("Filename")
                    r = requests.post(url, params=params, headers=headers, timeout=15)
            else:
                r = requests.post(url, params=params, headers=headers, timeout=15)
            if r.status_code >= 300:
                self._logger.warning("ntfy answered %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as e:
            self._logger.warning("ntfy notification failed: %s", e)
            return False
