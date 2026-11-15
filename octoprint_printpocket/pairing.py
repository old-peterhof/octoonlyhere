"""Short pairing codes, so a phone never has to type an OctoPrint password.

Someone already signed in to OctoPrint (usually on a computer) asks for a
code. The phone types it in and gets an application key for that same user.
Codes are single use, expire after ten minutes, and wrong guesses are capped.
"""

import secrets
import threading
import time

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
CODE_LENGTH = 6
CODE_TTL = 10 * 60
MAX_FAILURES = 20  # per FAILURE_WINDOW, across all clients
FAILURE_WINDOW = 10 * 60


def normalize(code):
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


class PairingCodes:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._codes = {}  # code -> (user_id, expires)
        self._failures = []

    def _prune(self, now):
        self._codes = {c: v for c, v in self._codes.items() if v[1] > now}
        self._failures = [t for t in self._failures if t > now - FAILURE_WINDOW]

    def create(self, user_id):
        with self._lock:
            now = self._clock()
            self._prune(now)
            # One live code per user: asking again replaces the old one.
            self._codes = {c: v for c, v in self._codes.items() if v[0] != user_id}
            code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
            self._codes[code] = (user_id, now + CODE_TTL)
            return code, CODE_TTL

    def claim(self, code):
        """Returns the user id for a valid code and burns it.

        Raises LockedOut when too many wrong codes were tried recently.
        """
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._failures) >= MAX_FAILURES:
                raise LockedOut()
            entry = self._codes.pop(normalize(code), None)
            if entry is None:
                self._failures.append(now)
                return None
            return entry[0]


class LockedOut(Exception):
    pass
