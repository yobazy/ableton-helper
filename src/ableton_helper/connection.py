"""Client for the AbletonHelper Remote Script (newline-delimited JSON over TCP)."""

from __future__ import annotations

import itertools
import json
import os
import socket
import threading
from typing import Any

HOST = os.environ.get("ABLETON_HELPER_HOST", "127.0.0.1")
PORT = int(os.environ.get("ABLETON_HELPER_PORT", "9878"))


class LiveError(RuntimeError):
    """Live reported an error, or couldn't be reached."""


NOT_RUNNING = (
    "Can't reach Ableton Live on {host}:{port}. Check that Live is open and that "
    "'AbletonHelper' is selected as a Control Surface in Preferences → Link, Tempo & MIDI "
    "(install it with `uv run ableton-helper install`)."
)


class LiveConnection:
    def __init__(self, host: str = HOST, port: int = PORT, timeout: float = 150.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._reader = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def _connect(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=3.0)
        except OSError as e:
            raise LiveError(NOT_RUNNING.format(host=self.host, port=self.port)) from e
        sock.settimeout(self.timeout)
        self._sock = sock
        self._reader = sock.makefile("rb")

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self) -> None:
        for thing in (self._reader, self._sock):
            try:
                if thing is not None:
                    thing.close()
            except OSError:
                pass
        self._sock = self._reader = None

    def send(self, kind: str, params: dict[str, Any] | None = None) -> Any:
        payload = json.dumps({"id": next(self._ids), "type": kind, "params": params or {}})
        data = payload.encode("utf-8") + b"\n"
        with self._lock:
            # A stale socket (Live restarted) fails on send, so reconnect and
            # resend once. Never resend after the request went out: a batch
            # that ran but whose reply got lost must not run twice.
            try:
                if self._sock is None:
                    self._connect()
                self._sock.sendall(data)
            except OSError:
                self._close()
                self._connect()
                self._sock.sendall(data)
            try:
                line = self._reader.readline()
            except OSError as e:
                self._close()
                raise LiveError(f"No reply from Live ({e}). Check the set before retrying.") from e
            if not line:
                self._close()
                raise LiveError("Live closed the connection before replying.")
        response = json.loads(line)
        if response.get("status") != "success":
            raise LiveError(response.get("message", "Unknown error from Live"))
        return response.get("result")

    # Convenience wrappers
    def ping(self) -> dict[str, Any]:
        return self.send("ping")

    def snapshot(self, include_notes: bool = False, include_devices: bool = True) -> dict[str, Any]:
        return self.send("snapshot", {"include_notes": include_notes, "include_devices": include_devices})

    def run_ops(self, ops: list[dict[str, Any]], stop_on_error: bool = False) -> dict[str, Any]:
        return self.send("run_ops", {"ops": ops, "stop_on_error": stop_on_error})
