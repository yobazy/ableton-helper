"""AbletonHelper MIDI Remote Script.

Listens on 127.0.0.1:9878 for newline-delimited JSON commands and runs them on
Live's main thread. Socket and threading approach adapted from ahujasid/ableton-mcp
(MIT, see THIRD_PARTY_NOTICES.md); the command set is our own and centres on
``run_ops``, which executes a whole batch of arrangement operations in one
main-thread tick instead of one request (and one tick) per change.

Install with ``uv run ableton-helper install``, then pick "AbletonHelper" as a
Control Surface in Live's Preferences → Link, Tempo & MIDI.
"""

from __future__ import absolute_import, print_function, unicode_literals

import json
import os
import socket
import threading
import time
import traceback

try:
    import queue
except ImportError:  # pragma: no cover - Python 2
    import Queue as queue

from _Framework.ControlSurface import ControlSurface

from .lom import Executor, read_notes, snapshot

SCRIPT_VERSION = "0.1.0"
HOST = os.environ.get("ABLETON_HELPER_HOST", "127.0.0.1")
PORT = int(os.environ.get("ABLETON_HELPER_PORT", "9878"))
MAX_LINE_BYTES = 32 * 1024 * 1024
MAIN_THREAD_TIMEOUT = 120.0


def _note_spec_factory():
    try:
        import Live
        return Live.Clip.MidiNoteSpecification
    except Exception:
        return None


def create_instance(c_instance):
    return AbletonHelper(c_instance)


class AbletonHelper(ControlSurface):

    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._song = self.song()
        self._executor = Executor(self._song, self.application(), _note_spec_factory())
        self._running = True
        self._server = None
        self._start_server()
        self.show_message("AbletonHelper: listening on port %d" % PORT)

    def disconnect(self):
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
        ControlSurface.disconnect(self)

    # ── socket server ───────────────────────────────────────────────────────

    def _start_server(self):
        try:
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind((HOST, PORT))
            self._server.listen(4)
            self._server.settimeout(1.0)
        except Exception as e:
            self.log_message("AbletonHelper: could not start server: %s" % e)
            self.show_message("AbletonHelper: port %d unavailable" % PORT)
            return
        thread = threading.Thread(target=self._accept_loop)
        thread.daemon = True
        thread.start()

    def _accept_loop(self):
        while self._running:
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except Exception:
                if self._running:
                    time.sleep(0.5)
                continue
            thread = threading.Thread(target=self._client_loop, args=(client,))
            thread.daemon = True
            thread.start()

    def _client_loop(self, client):
        buffer = b""
        try:
            while self._running:
                data = client.recv(65536)
                if not data:
                    break
                buffer += data
                if len(buffer) > MAX_LINE_BYTES:
                    raise ValueError("Request too large")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    response = self._handle_line(line)
                    client.sendall(json.dumps(response).encode("utf-8") + b"\n")
        except Exception as e:
            self.log_message("AbletonHelper client error: %s" % e)
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _handle_line(self, line):
        try:
            command = json.loads(line.decode("utf-8"))
        except Exception as e:
            return {"status": "error", "message": "Bad JSON: %s" % e}
        request_id = command.get("id")
        try:
            result = self._on_main_thread(command.get("type"), command.get("params") or {})
            response = {"status": "success", "result": result}
        except Exception as e:
            self.log_message(traceback.format_exc())
            response = {"status": "error", "message": str(e)}
        if request_id is not None:
            response["id"] = request_id
        return response

    def _on_main_thread(self, kind, params):
        """Run a command inside Live's main thread and wait for the result.

        The Live API is not thread-safe, so every read and write goes through
        schedule_message. One command = one tick, which is why batching matters.
        """
        replies = queue.Queue()

        def task():
            try:
                replies.put(("ok", self._dispatch(kind, params)))
            except Exception as e:
                self.log_message(traceback.format_exc())
                replies.put(("error", str(e)))

        try:
            self.schedule_message(0, task)
        except AssertionError:
            task()
        try:
            status, value = replies.get(timeout=MAIN_THREAD_TIMEOUT)
        except queue.Empty:
            raise RuntimeError("Timed out waiting for Live's main thread")
        if status == "error":
            raise RuntimeError(value)
        return value

    # ── commands ────────────────────────────────────────────────────────────

    def _dispatch(self, kind, params):
        if kind == "ping":
            return {"name": "AbletonHelper", "version": SCRIPT_VERSION,
                    "live_version": self._live_version()}
        if kind == "snapshot":
            return snapshot(self._song,
                            include_notes=bool(params.get("include_notes", False)),
                            include_devices=bool(params.get("include_devices", True)))
        if kind == "run_ops":
            started = time.time()
            result = self._executor.run(params.get("ops", []),
                                        stop_on_error=bool(params.get("stop_on_error", False)))
            result["main_thread_ms"] = round((time.time() - started) * 1000.0, 1)
            return result
        if kind == "clip_notes":
            track = self._song.tracks[int(params["track"])]
            slot = track.clip_slots[int(params["slot"])]
            if not slot.has_clip:
                raise ValueError("No clip in that slot")
            return {"name": slot.clip.name, "length": float(slot.clip.length),
                    "notes": read_notes(slot.clip)}
        raise ValueError("Unknown command: %s" % kind)

    def _live_version(self):
        try:
            app = self.application()
            return "%d.%d.%d" % (app.get_major_version(), app.get_minor_version(),
                                 app.get_bugfix_version())
        except Exception:
            return "unknown"
