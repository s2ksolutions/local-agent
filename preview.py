# preview.py
"""Read-only localhost preview servers for workspace web projects.

This module does not execute project code on the server, expose a shell, or
provide POST/PUT/DELETE APIs. Application/runtime wiring is supplied separately.
It is intended for trusted local use, not public hosting or hostile local races.
"""
import json
import mimetypes
import socket
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


class PreviewError(ValueError):
    pass


def _is_link(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _relative_parts(value):
    if not isinstance(value, str):
        raise PreviewError("Preview path must be a string")
    if any(char in value for char in ("\\", "\x00", "\r", "\n", ":")):
        raise PreviewError("Invalid preview path")
    if value.startswith("/"):
        value = value[1:]
    value = value.rstrip("/")
    if not value:
        return []
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PreviewError("Invalid preview path")
    if any(part.startswith(".") for part in parts):
        raise PreviewError("Hidden paths are not served")
    return parts


def _owner(value, workspace):
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("workspace") != workspace:
        raise PreviewError("Owner workspace does not match")
    return {
        key: value.get(key)
        for key in ("workspace", "conversation", "run", "agent")
    }


class _PreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class _PreviewHandler(BaseHTTPRequestHandler):
    server_version = "LocalAgentPreview/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Structured records are emitted instead of raw terminal request logs.
        pass

    def do_GET(self):
        self._serve(head=False)

    def do_HEAD(self):
        self._serve(head=True)

    def do_POST(self):
        self._deny_method()

    def do_PUT(self):
        self._deny_method()

    def do_PATCH(self):
        self._deny_method()

    def do_DELETE(self):
        self._deny_method()

    def do_OPTIONS(self):
        self._deny_method()

    def do_TRACE(self):
        self._deny_method()

    def _valid_host(self):
        expected = {
            "127.0.0.1:" + str(self.server.server_port),
            "localhost:" + str(self.server.server_port),
        }
        return self.headers.get("Host", "").lower() in expected

    def _headers(self, status, content_type, size, location=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Connection", "close")
        if location is not None:
            self.send_header("Location", location)
        if status == 405:
            self.send_header("Allow", "GET, HEAD")
        self.end_headers()
        self.close_connection = True

    def _record(self, status, size=0, error=None):
        self.server.manager._request_finished(
            self.server.workspace,
            self.server.preview_id,
            self.command,
            urlsplit(self.path).path[:2048],
            status,
            size,
            error,
        )

    def _text(self, status, text, head=False):
        body = text.encode("utf-8")
        self._headers(status, "text/plain; charset=utf-8", len(body))
        if not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        self._record(status, 0 if head else len(body))

    def _deny_method(self):
        if not self._valid_host():
            self._text(403, "Local preview host denied.")
            return
        self._text(405, "Read-only preview: only GET and HEAD are supported.")

    def _serve(self, head=False):
        if not self._valid_host():
            self._text(403, "Local preview host denied.", head)
            return
        try:
            requested = urlsplit(self.path)
            if requested.scheme or requested.netloc:
                raise PreviewError("Absolute request targets are not allowed")
            decoded = unquote(requested.path, encoding="utf-8", errors="strict")
            parts = _relative_parts(decoded)
            manager = self.server.manager
            root = manager._workspace_root(self.server.workspace)
            if root != self.server.root:
                raise PreviewError("Workspace preview root changed")
            path = manager._resolve(root, parts)
            if path.is_dir():
                if parts and not requested.path.endswith("/"):
                    location = "/" + "/".join(quote(part, safe="") for part in parts) + "/"
                    # Query values are not copied into preview redirects or logs.
                    self._headers(308, "text/plain; charset=utf-8", 0, location)
                    self._record(308)
                    return
                path = manager._index(root, path)
            manager._check_file(path)
            with path.open("rb") as stream:
                size = path.stat().st_size
                if size > manager.max_file_bytes:
                    self._text(413, "File exceeds the preview size limit.", head)
                    return
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if path.suffix.lower() in (".js", ".mjs"):
                    mime = "text/javascript"
                if mime.startswith("text/") or mime in ("application/json", "image/svg+xml"):
                    mime += "; charset=utf-8"
                self._headers(200, mime, size)
                written = 0
                if not head:
                    while True:
                        chunk = stream.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        written += len(chunk)
                self._record(200, written)
        except (BrokenPipeError, ConnectionResetError):
            self._record(499, error="Client disconnected")
        except (PreviewError, UnicodeError, ValueError):
            self._text(403, "Preview path denied.", head)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            self._text(404, "Preview file not found. Directory listing is disabled.", head)
        except PermissionError:
            self._text(403, "Preview file access denied.", head)
        except OSError:
            self._text(500, "Preview could not read the requested file.", head)


class PreviewManager:
    def __init__(self, store, max_servers=4, max_file_bytes=10 * 1024 * 1024):
        self.s = store
        self.max_servers = max(1, min(int(max_servers), 16))
        self.max_file_bytes = max(1024, int(max_file_bytes))
        self._lock = threading.RLock()
        self._servers = {}
        self._events = deque(maxlen=2000)
        self._sequence = 0
        self._sink = None
        self.last_sink_error = None

    def set_event_sink(self, callback):
        """Set an optional synchronous callback(record) for event persistence."""
        if callback is not None and not callable(callback):
            raise PreviewError("Event sink must be callable")
        self._sink = callback

    def _emit(self, event_type, workspace, body):
        with self._lock:
            self._sequence += 1
            record = {
                "seq": self._sequence,
                "event_id": uuid.uuid4().hex,
                "type": event_type,
                "workspace": workspace,
                "created": time.time(),
                "body": body,
            }
            self._events.append(record)
        callback = self._sink
        if callback:
            try:
                callback(json.loads(json.dumps(record)))
                self.last_sink_error = None
            except Exception as error:
                self.last_sink_error = str(error)[:1000]
        return record

    def read_events(self, workspace=None, after=0, limit=1000):
        if workspace:
            self.s.get("workspace", workspace)
        with self._lock:
            rows = [event for event in self._events if event["seq"] > after
                    and (workspace is None or event["workspace"] == workspace)]
            return json.loads(json.dumps(rows[:max(1, min(int(limit), 2000))]))

    def _workspace_root(self, workspace):
        self.s.get("workspace", workspace)
        if not isinstance(workspace, str) or not workspace or "/" in workspace or "\\" in workspace:
            raise PreviewError("Invalid workspace ID")
        projects = getattr(self.s, "projects", None)
        if projects is not None:
            candidate = Path(projects) / workspace
            if _is_link(candidate):
                raise PreviewError("Workspace root links are not allowed")
        root = Path(self.s.project(workspace))
        if _is_link(root) or not root.is_dir():
            raise PreviewError("Workspace root unavailable")
        return root.resolve()

    def _resolve(self, root, parts):
        path = root
        for part in parts:
            path = path / part
            if _is_link(path):
                raise PreviewError("Preview links and junctions are denied")
        resolved = path.resolve()
        if resolved != root and not resolved.is_relative_to(root):
            raise PreviewError("Preview path is outside the workspace")
        return resolved

    def _index(self, root, directory):
        for name in ("index.html", "index.htm"):
            candidate = directory / name
            if _is_link(candidate):
                raise PreviewError("Preview links are denied")
            if candidate.is_file():
                return self._resolve(root, list(candidate.relative_to(root).parts))
        raise FileNotFoundError("No index document")

    def _check_file(self, path):
        if _is_link(path):
            raise PreviewError("Preview links are denied")
        if not path.is_file():
            raise FileNotFoundError("Preview file not found")
        if path.suffix.lower() in (".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx"):
            raise PreviewError("Sensitive file type is not served")

    def _metadata(self, entry, relative_path=""):
        origin = "http://127.0.0.1:" + str(entry["server"].server_port)
        encoded = "/".join(quote(part, safe="") for part in _relative_parts(relative_path))
        return {
            "workspace": entry["workspace"],
            "preview_id": entry["preview_id"],
            "origin": origin,
            "url": origin + "/" + encoded,
            "created": entry["created"],
            "last_access": entry["last_access"],
            "requests": entry["requests"],
            "running": entry["thread"].is_alive(),
            "read_only": True,
            "owner": entry["owner"],
        }

    def start(self, workspace, path="", owner=None):
        root = self._workspace_root(workspace)
        owner = _owner(owner, workspace)
        parts = _relative_parts(path)
        requested = self._resolve(root, parts)
        if requested.is_dir():
            self._check_file(self._index(root, requested))
        else:
            self._check_file(requested)
        with self._lock:
            existing = self._servers.get(workspace)
            if existing and existing["thread"].is_alive():
                existing["last_access"] = time.time()
                return self._metadata(existing, path)
            if len(self._servers) >= self.max_servers:
                raise PreviewError("Preview server limit reached; stop an unused preview")
            server = _PreviewServer(("127.0.0.1", 0), _PreviewHandler)
            server.manager = self
            server.workspace = workspace
            server.preview_id = uuid.uuid4().hex
            server.root = root
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .2}, daemon=True)
            entry = {
                "workspace": workspace,
                "preview_id": server.preview_id,
                "server": server,
                "thread": thread,
                "created": time.time(),
                "last_access": time.time(),
                "requests": 0,
                "owner": owner,
            }
            self._servers[workspace] = entry
            thread.start()
            metadata = self._metadata(entry, path)
        self._emit("preview.started", workspace, metadata)
        return metadata

    def url(self, workspace, path=""):
        parts = _relative_parts(path)
        root = self._workspace_root(workspace)
        requested = self._resolve(root, parts)
        if requested.is_dir():
            self._check_file(self._index(root, requested))
        else:
            self._check_file(requested)
        with self._lock:
            entry = self._servers.get(workspace)
            if not entry or not entry["thread"].is_alive():
                raise PreviewError("Workspace preview is not running")
            return self._metadata(entry, path)["url"]

    def list(self, workspace=None):
        if workspace:
            self.s.get("workspace", workspace)
        with self._lock:
            return [self._metadata(entry) for entry in self._servers.values()
                    if workspace is None or entry["workspace"] == workspace]

    def _request_finished(self, workspace, preview_id, method, path, status, size, error=None):
        with self._lock:
            entry = self._servers.get(workspace)
            if entry and entry["preview_id"] == preview_id:
                entry["last_access"] = time.time()
                entry["requests"] += 1
        self._emit("preview.request", workspace, {
            "preview_id": preview_id, "method": method,
            "path": path, "status": status,
            "bytes_sent": size, "error": error,
        })

    def stop(self, workspace):
        self.s.get("workspace", workspace)
        with self._lock:
            entry = self._servers.pop(workspace, None)
        if not entry:
            return {"workspace": workspace, "stopped": False}
        entry["server"].shutdown()
        entry["server"].server_close()
        entry["thread"].join(timeout=3)
        result = {"workspace": workspace, "preview_id": entry["preview_id"], "stopped": True}
        self._emit("preview.stopped", workspace, result)
        return result

    def cleanup_idle(self, max_idle=3600):
        max_idle = max(60, float(max_idle))
        now = time.time()
        with self._lock:
            expired = [entry["workspace"] for entry in self._servers.values()
                       if now - entry["last_access"] >= max_idle]
        return [self.stop(workspace) for workspace in expired]

    def close(self):
        with self._lock:
            workspaces = list(self._servers)
        return [self.stop(workspace) for workspace in workspaces]
