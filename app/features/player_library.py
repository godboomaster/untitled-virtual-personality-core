"""Short-lived control connection to the audio element in an open web player.

No audio is uploaded: the browser reports status and acknowledges commands.
An expired tab cannot intercept commands intended for the PC music browser.
"""
import threading
import time
import uuid

from app.features.music_browser import MusicBrowserError


class LibraryPlayer:
    def __init__(self):
        self._cv = threading.Condition()
        self._owner = None
        self._until = 0
        self._status = None
        self._commands = {}

    def status(self):
        with self._cv:
            if self._owner and time.monotonic() < self._until:
                return dict(self._status)
            return None

    def deactivate(self):
        with self._cv:
            self._owner = None
            self._cv.notify_all()

    def update(self, client, status, *, activate=False, ack=None, error=""):
        with self._cv:
            if activate and status.get('state') != 'stopped':
                self._owner = client
                self._commands.clear()
            if self._owner != client or ((not activate or status.get('state') == 'stopped') and time.monotonic() >= self._until):
                return {"active": False, "commands": []}
            self._until = time.monotonic() + 8
            self._status = {"source": "library", "running": True, "headless": False,
                "url": "", "muted": False, "error": "", **status}
            if ack in self._commands:
                self._commands[ack]["done"] = True
                self._commands[ack]["error"] = error
                self._cv.notify_all()
            if status.get('state') == 'stopped':
                self._status['running'] = False
                self._owner = None
                self._cv.notify_all()
                return {"active": False, "commands": [], "finished": True}
            commands = []
            for row in self._commands.values():
                if not row["sent"] and not row["done"]:
                    row["sent"] = True
                    commands.append({k: row[k] for k in ("id", "command", "value")})
            return {"active": True, "commands": commands}

    def control(self, command, value=None):
        with self._cv:
            if self.status() is None:
                raise MusicBrowserError("Вкладка с музыкой закрыта. Откройте плеер.")
            owner = self._owner
            key = uuid.uuid4().hex
            row = {"id": key, "command": command, "value": value,
                   "sent": False, "done": False, "error": ""}
            self._commands[key] = row
            deadline = time.monotonic() + 6
            try:
                while not row["done"] and self._owner == owner and time.monotonic() < deadline:
                    self._cv.wait(max(.01, deadline - time.monotonic()))
                if not row["done"]:
                    raise MusicBrowserError("Вкладка с музыкой не ответила. Откройте плеер и повторите команду.")
                if row["error"]:
                    raise MusicBrowserError(row["error"])
                result = dict(self._status)
                if command == "stop":
                    self._owner = None
                    result.update(running=False, state="stopped")
                return result
            finally:
                self._commands.pop(key, None)


library_player = LibraryPlayer()
