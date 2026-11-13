# browser.py
"""Managed Chromium transport, tab registry and continuous browser event capture.

Drop-in compatibility with the existing Browser API is retained. Agent tool/API
wiring is supplied by the other application files, not by this module alone.
Console text and ordinary URLs may contain private data; redaction is best-effort.
"""
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

class BrowserError(ValueError):
    pass

def _uid():
    return uuid.uuid4().hex

def _sensitive(key):
    key = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(part in key for part in (
        "authorization", "cookie", "password", "secret", "apikey", "token", "credential",
        "accesstoken", "refreshtoken", "csrftoken", "sessionid",
    )) or key in {"token", "auth", "session", "key", "code"}

def _safe_url(value):
    try:
        parsed = urlsplit(str(value))
        host = parsed.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port:
            host += ":" + str(parsed.port)
        query = urlencode([
            (key, "[redacted]" if _sensitive(key) else val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        ])
        fragment = parsed.fragment
        if re.search(r"token=|password=|secret=|authorization=", fragment, re.I):
            fragment = "[redacted]"
        return urlunsplit((parsed.scheme, host, parsed.path, query, fragment))[:4096]
    except (TypeError, ValueError):
        return "[invalid URL]"

def _redact(value, key=""):
    if _sensitive(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value[:100]]
    if isinstance(value, str):
        if key.lower() in {"url", "documenturl", "referer", "location"}:
            return _safe_url(value)
        return value[:8192] + (" [truncated]" if len(value) > 8192 else "")
    return value

def _owner(owner):
    if owner is None:
        return None
    if not isinstance(owner, dict) or not owner.get("workspace"):
        raise BrowserError("An owner requires a workspace ID")
    if not owner.get("conversation") and not owner.get("run"):
        raise BrowserError("An owner requires a conversation or run ID")
    return {key: owner.get(key) for key in ("workspace", "conversation", "run", "agent")}

def _same_scope(left, right):
    if not left or not right or left.get("workspace") != right.get("workspace"):
        return False
    if left.get("conversation") or right.get("conversation"):
        return bool(left.get("conversation")) and left.get("conversation") == right.get("conversation")
    return bool(left.get("run")) and left.get("run") == right.get("run")

class _CDPSession:
    """One websocket reader; command callers never consume each other's events."""
    def __init__(self, url, on_event, on_disconnect=None):
        import websocket
        self._websocket = websocket
        self.ws = websocket.create_connection(
            url, timeout=1, suppress_origin=True,
            http_proxy_host=None, http_proxy_port=None,
        )
        self._on_event = on_event
        self._on_disconnect = on_disconnect
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._pending = {}
        self._sequence = 0
        self._closed = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    @property
    def closed(self):
        return self._closed.is_set()

    def call(self, method, params=None, timeout=25):
        with self._lock:
            if self.closed:
                raise BrowserError("Browser connection is closed")
            self._sequence += 1
            command_id = self._sequence
            pending = {"ready": threading.Event(), "response": None, "error": None}
            self._pending[command_id] = pending
        try:
            with self._send_lock:
                self.ws.send(json.dumps({
                    "id": command_id, "method": method, "params": params or {},
                }))
            if not pending["ready"].wait(timeout):
                raise BrowserError("Browser command timed out: " + method)
            if pending["error"]:
                raise BrowserError(pending["error"])
            response = pending["response"] or {}
            if "error" in response:
                raise BrowserError(method + ": " + json.dumps(response["error"]))
            return response.get("result", {})
        finally:
            with self._lock:
                self._pending.pop(command_id, None)

    def _read(self):
        reason = "Browser websocket closed"
        try:
            while not self.closed:
                try:
                    raw = self.ws.recv()
                except self._websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    break
                message = json.loads(raw)
                if "id" in message:
                    with self._lock:
                        pending = self._pending.get(message["id"])
                        if pending:
                            pending["response"] = message
                            pending["ready"].set()
                elif "method" in message:
                    try:
                        self._on_event(message["method"], message.get("params", {}))
                    except Exception:
                        pass
        except Exception as error:
            reason = str(error)
        finally:
            self._closed.set()
            with self._lock:
                for pending in self._pending.values():
                    pending["error"] = reason
                    pending["ready"].set()
            if self._on_disconnect:
                try:
                    self._on_disconnect(reason)
                except Exception:
                    pass

    def close(self):
        self._closed.set()
        try:
            self.ws.close()
        except Exception:
            pass
        with self._lock:
            for pending in self._pending.values():
                pending["error"] = "Browser connection closed"
                pending["ready"].set()

class Browser:
    def __init__(self, data):
        self.data = Path(data)
        self.lock = threading.RLock()
        self._registry_lock = threading.RLock()
        self._event_lock = threading.RLock()
        self.process = None
        self.port = None
        self.selected = None
        self.ws = None
        self.seq = 0
        self.events = []
        self.session_id = None
        self.engine = None
        self.visible = False
        self._browser = None
        self._pages = {}
        self._tabs = {}
        self._attach_locks = {}
        self._requests = {}
        self._last_actions = {}
        self._event_sequence = 0
        self._sink = None
        self._sink_queue = queue.Queue(maxsize=10000)
        self._sink_dropped = 0
        self.capture_headers = False
        self.capture_post_data = False
        threading.Thread(target=self._dispatch_events, daemon=True).start()

    def set_event_sink(self, callback):
        """Optional callback(record) for persistence; dispatched off reader threads."""
        if callback is not None and not callable(callback):
            raise BrowserError("Event sink must be callable")
        self._sink = callback

    def _dispatch_events(self):
        while True:
            record = self._sink_queue.get()
            try:
                callback = self._sink
                if callback:
                    callback(record)
            except Exception as error:
                with self._event_lock:
                    self._event_sequence += 1
                    self.events.append({
                        "seq": self._event_sequence,
                        "event_id": _uid(), "session_id": self.session_id,
                        "tab_id": None, "tab_seq": None,
                        "type": "browser.persistence_error", "created": time.time(),
                        "body": {"error": str(error)[:1000]}, "owner": None,
                    })
                    del self.events[:-5000]
            finally:
                self._sink_queue.task_done()

    def _emit(self, event_type, body=None, tab_id=None, session_id=None):
        with self._registry_lock:
            tab = self._tabs.get(tab_id)
            owner = dict(tab["owner"]) if tab and tab.get("owner") else None
            record_session = session_id or (tab.get("session_id") if tab else self.session_id)
            tab_sequence = None
            if tab:
                tab["event_sequence"] = tab.get("event_sequence", 0) + 1
                tab_sequence = tab["event_sequence"]
        with self._event_lock:
            self._event_sequence += 1
            record = {
                "seq": self._event_sequence, "event_id": _uid(),
                "session_id": record_session, "tab_id": tab_id,
                "tab_seq": tab_sequence, "owner": owner,
                "type": event_type, "created": time.time(),
                "body": _redact(body or {}),
            }
            if self._sink_dropped:
                record["body"]["persistence_records_dropped"] = self._sink_dropped
            self.events.append(record)
            del self.events[:-5000]
            if self._sink:
                try:
                    self._sink_queue.put_nowait(record)
                    self._sink_dropped = 0
                except queue.Full:
                    self._sink_dropped += 1
            return record

    def read_events(self, tab_id=None, after=0, owner=None, limit=1000):
        owner = _owner(owner)
        if tab_id:
            self._check_tab(tab_id, owner, allow_closed=True)
        with self._event_lock:
            rows = [event for event in self.events if event["seq"] > after]
            if tab_id:
                rows = [event for event in rows if event["tab_id"] == tab_id]
            if owner:
                rows = [event for event in rows if _same_scope(event.get("owner"), owner)]
            return json.loads(json.dumps(rows[:max(1, min(int(limit), 5000))]))

    def _json(self, path):
        if not self.port:
            raise BrowserError("Browser not started")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:" + str(self.port) + path, timeout=5) as response:
            return json.load(response)

    def _executable(self, engine):
        if engine not in ("edge", "chrome"):
            raise BrowserError("Choose Edge or Chrome")
        suffix = "Microsoft/Edge/Application/msedge.exe" if engine == "edge" else "Google/Chrome/Application/chrome.exe"
        candidates = [Path(os.getenv(key, "")) / suffix for key in (
            "PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA",
        ) if os.getenv(key)]
        names = ("msedge", "microsoft-edge", "microsoft-edge-stable") if engine == "edge" else ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
        candidates.extend(Path(path) for name in names if (path := shutil.which(name)))
        candidates.append(Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge") if engine == "edge" else Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        raise BrowserError(engine + " executable not found")

    def start(self, engine="edge", visible=False, owner=None):
        owner = _owner(owner)
        with self.lock:
            if self.process and self.process.poll() is None:
                if not self._browser or self._browser.closed:
                    self._connect_browser()
                if owner and not self.tabs(owner=owner):
                    self.open_tab("about:blank", label="Task tab", purpose="Task", owner=owner)
                return self.tabs(owner=owner)
            executable = self._executable(engine)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                self.port = sock.getsockname()[1]
            self.session_id = _uid()
            self.engine = engine
            self.visible = bool(visible)
            profile = self.data / "browser-profiles" / engine
            profile.mkdir(parents=True, exist_ok=True)
            args = [executable, "--remote-debugging-port=" + str(self.port),
                    "--remote-debugging-address=127.0.0.1",
                    "--user-data-dir=" + str(profile.resolve()),
                    "--no-first-run", "--no-default-browser-check"]
            if not visible:
                args += ["--headless=new", "--window-size=1280,800"]
            self.process = subprocess.Popen(args + ["about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(75):
                    if self.process.poll() is not None:
                        raise BrowserError("Browser exited during startup")
                    try:
                        self._json("/json/version")
                        break
                    except Exception:
                        time.sleep(.2)
                else:
                    raise BrowserError("Browser startup timed out")
                self._connect_browser()
                targets = self.targets()
                for target in targets:
                    self._register_target(target, owner=owner)
                    self._ensure_page(target["id"])
                if targets:
                    self.select(targets[0]["id"], owner=owner)
                self._emit("browser.started", {"engine": engine, "visible": bool(visible), "pid": self.process.pid})
                return self.tabs(owner=owner)
            except Exception:
                self.stop()
                raise

    def _connect_browser(self):
        version = self._json("/json/version")
        url = version.get("webSocketDebuggerUrl")
        if not url:
            raise BrowserError("Browser debugging websocket unavailable")
        if self._browser:
            self._browser.close()
        connected_session = self.session_id
        self._browser = _CDPSession(url, self._browser_event,
            lambda reason: self._emit("browser.disconnected", {"reason": reason}, session_id=connected_session))
        self._emit("browser.connected", {"browser": version.get("Browser"), "protocol": version.get("Protocol-Version")})
        self._browser.call("Target.setDiscoverTargets", {"discover": True})

    def targets(self):
        return [target for target in self._json("/json/list") if target.get("type") == "page"]

    def _register_target(self, target, owner=None):
        tab_id = target.get("id") or target.get("targetId")
        if not tab_id:
            return
        created = False
        with self._registry_lock:
            tab = self._tabs.get(tab_id)
            if not tab:
                opener = self._tabs.get(target.get("openerId"), {})
                inherited = owner or opener.get("owner")
                tab = {"id": tab_id, "session_id": self.session_id,
                       "owner": dict(inherited) if inherited else None,
                       "label": "Task tab" if inherited else "Browser tab",
                       "purpose": "Task" if inherited else "Manual",
                       "pinned": False, "created": time.time(),
                       "closed": False, "event_sequence": 0,
                       "capture_started": None, "last_action": None,
                       "screenshot_at": None, "error_count": 0}
                self._tabs[tab_id] = tab
                created = True
            elif owner and not tab.get("owner") and target.get("url") in ("", "about:blank"):
                tab["owner"] = dict(owner)
                tab["label"] = "Task tab"
                tab["purpose"] = "Task"
            tab.update(title=target.get("title", tab.get("title", "")),
                       url=target.get("url", tab.get("url", "about:blank")))
        if created:
            self._emit("browser.tab_created", {"title": tab.get("title"), "url": tab.get("url"), "opener_id": target.get("openerId")}, tab_id)

    def _browser_event(self, method, params):
        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            target = params.get("targetInfo", {})
            if target.get("type") != "page":
                return
            self._register_target(target)
            if method == "Target.targetCreated":
                threading.Thread(target=self._attach_background, args=(target["targetId"],), daemon=True).start()
        elif method == "Target.targetDestroyed":
            tab_id = params.get("targetId")
            with self._registry_lock:
                tab = self._tabs.get(tab_id)
                if tab:
                    tab["closed"] = True
                connection = self._pages.pop(tab_id, None)
                if self.selected == tab_id:
                    self.selected = None
                    self.ws = None
            if connection:
                connection.close()
            self._emit("browser.tab_closed", {}, tab_id)

    def _attach_background(self, tab_id):
        try:
            self._ensure_page(tab_id)
        except Exception as error:
            self._emit("browser.capture_unavailable", {"reason": str(error)}, tab_id)

    def _ensure_page(self, tab_id):
        with self._registry_lock:
            attach_lock = self._attach_locks.setdefault(tab_id, threading.Lock())
        with attach_lock:
            with self._registry_lock:
                current = self._pages.get(tab_id)
                if current and not current.closed:
                    return current
            target = next((item for item in self.targets() if item["id"] == tab_id), None)
            if not target:
                raise BrowserError("Tab is closed or unavailable: " + str(tab_id))
            self._register_target(target)
            connection = _CDPSession(target["webSocketDebuggerUrl"],
                lambda method, params: self._page_event(tab_id, method, params),
                lambda reason: self._emit("browser.tab_disconnected", {"reason": reason}, tab_id))
            try:
                for method in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable"):
                    connection.call(method)
                with self._registry_lock:
                    self._pages[tab_id] = connection
                    self._tabs[tab_id]["capture_started"] = time.time()
                self._emit("browser.capture_started", {"note": "Events before attachment were not captured; bodies are off by default"}, tab_id)
                return connection
            except Exception:
                connection.close()
                raise

    def _check_tab(self, tab_id=None, owner=None, allow_closed=False):
        owner = _owner(owner)
        tab_id = tab_id or self.selected
        if not tab_id:
            raise BrowserError("Select a tab or supply tab_id")
        with self._registry_lock:
            tab = self._tabs.get(tab_id)
            if not tab or tab.get("closed") and not allow_closed:
                raise BrowserError("Tab is closed or unknown: " + str(tab_id))
            if owner and not _same_scope(tab.get("owner"), owner):
                raise BrowserError("Tab belongs to a different conversation or run")
            return tab_id

    def tabs(self, owner=None, include_closed=False):
        owner = _owner(owner)
        if self.port and self.process and self.process.poll() is None:
            for target in self.targets():
                self._register_target(target)
        with self._registry_lock:
            result = [dict(tab) for tab in self._tabs.values()
                      if (include_closed or not tab.get("closed"))
                      and (owner is None or _same_scope(tab.get("owner"), owner))]
        result = json.loads(json.dumps(result))
        for tab in result:
            connection = self._pages.get(tab["id"])
            tab["url"] = _safe_url(tab.get("url", ""))
            tab["selected"] = tab["id"] == self.selected
            tab["capture_state"] = "closed" if tab.get("closed") else "capturing" if tab.get("capture_started") and connection and not connection.closed else "not_attached"
        return sorted(result, key=lambda tab: tab["created"])

    def select(self, tab_id, owner=None):
        tab_id = self._check_tab(tab_id, owner)
        connection = self._ensure_page(tab_id)
        with self._registry_lock:
            self.selected = tab_id
            self.ws = connection.ws
        self._emit("browser.tab_selected", {}, tab_id)
        return {"selected": tab_id, "tab_id": tab_id}

    def set_tab_context(self, tab_id, label=None, purpose=None, pinned=None, owner=None):
        tab_id = self._check_tab(tab_id, owner)
        with self._registry_lock:
            tab = self._tabs[tab_id]
            if label is not None:
                tab["label"] = str(label)[:100]
            if purpose is not None:
                tab["purpose"] = str(purpose)[:300]
            if pinned is not None:
                tab["pinned"] = bool(pinned)
            result = dict(tab)
        self._emit("browser.tab_context_changed", {"label": result["label"], "purpose": result["purpose"], "pinned": result["pinned"]}, tab_id)
        return result

    def _url(self, url, blank=False):
        if blank and url == "about:blank":
            return url
        parsed = urlsplit(str(url))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise BrowserError("Only HTTP(S) page URLs are allowed")
        if parsed.username or parsed.password:
            raise BrowserError("Credentials in page URLs are not allowed")
        return str(url)

    def open_tab(self, url="about:blank", label="Task tab", purpose="Task", owner=None, select=True):
        owner = _owner(owner)
        url = self._url(url, blank=True)
        with self.lock:
            if not self.process or self.process.poll() is not None:
                self.start()
            if not self._browser or self._browser.closed:
                self._connect_browser()
            result = self._browser.call("Target.createTarget", {"url": "about:blank"})
            tab_id = result["targetId"]
            self._register_target({"id": tab_id, "url": "about:blank", "title": ""}, owner=owner)
            with self._registry_lock:
                self._tabs[tab_id]["owner"] = dict(owner) if owner else None
            self.set_tab_context(tab_id, label, purpose, owner=owner)
            self._ensure_page(tab_id)
            if select:
                self.select(tab_id, owner)
            if url != "about:blank":
                self.navigate(url, tab_id=tab_id, owner=owner)
            return next(tab for tab in self.tabs(owner=owner) if tab["id"] == tab_id)

    def new_tab(self, owner=None):
        self.open_tab(owner=owner)
        return self.tabs(owner=owner)

    def _begin_action(self, tab_id, name, arguments):
        action = {"id": _uid(), "name": name, "started": time.time()}
        with self._registry_lock:
            self._last_actions[tab_id] = action
            self._tabs[tab_id]["last_action"] = {**action, "status": "started"}
        self._emit("browser.action_started", {"action_id": action["id"], "action": name, "arguments": arguments}, tab_id)
        return action

    def _finish_action(self, tab_id, action, result=None, error=None):
        status = "failed" if error else "succeeded"
        with self._registry_lock:
            if tab_id in self._tabs:
                self._tabs[tab_id]["last_action"] = {**action, "status": status, "finished": time.time()}
        self._emit("browser.action_failed" if error else "browser.action_completed",
                   {"action_id": action["id"], "action": action["name"],
                    "result": result, "error": str(error) if error else None}, tab_id)

    def rpc(self, method, params=None, tab_id=None, owner=None):
        tab_id = self._check_tab(tab_id, owner)
        return self._ensure_page(tab_id).call(method, params)

    def read(self, tab_id=None, owner=None):
        tab_id = self._check_tab(tab_id, owner)
        action = self._begin_action(tab_id, "read_page", {})
        try:
            result = self.rpc("Runtime.evaluate", {
                "expression": "JSON.stringify({url:location.href,title:document.title,text:document.body?document.body.innerText:'',readyState:document.readyState})",
                "returnByValue": True,
            }, tab_id, owner)
            if result.get("exceptionDetails"):
                raise BrowserError(json.dumps(result["exceptionDetails"]))
            page = json.loads(result["result"]["value"])
            self._finish_action(tab_id, action, {"url": page["url"], "title": page["title"]})
            return {"tab_id": tab_id, **page}
        except Exception as error:
            self._finish_action(tab_id, action, error=error)
            raise

    def navigate(self, url, tab_id=None, owner=None):
        url = self._url(url)
        tab_id = self._check_tab(tab_id, owner)
        action = self._begin_action(tab_id, "navigate", {"url": url})
        try:
            result = self.rpc("Page.navigate", {"url": url}, tab_id, owner)
            if result.get("errorText"):
                raise BrowserError(result["errorText"])
            self._finish_action(tab_id, action, result)
            return {"tab_id": tab_id, "action_id": action["id"], "result": result, "note": "Navigation initiated; page load and application success not yet verified"}
        except Exception as error:
            self._finish_action(tab_id, action, error=error)
            raise

    def reload(self, tab_id=None, owner=None, ignore_cache=True):
        tab_id = self._check_tab(tab_id, owner)
        action = self._begin_action(tab_id, "reload", {"ignore_cache": bool(ignore_cache)})
        try:
            result = self.rpc("Page.reload", {"ignoreCache": bool(ignore_cache)}, tab_id, owner)
            self._finish_action(tab_id, action, result)
            return {"tab_id": tab_id, "action_id": action["id"], "note": "Reload initiated; verify the resulting page and diagnostics"}
        except Exception as error:
            self._finish_action(tab_id, action, error=error)
            raise

    def screenshot(self, tab_id=None, owner=None):
        tab_id = self._check_tab(tab_id, owner)
        result = self.rpc("Page.captureScreenshot", {"format": "jpeg", "quality": 75}, tab_id, owner)
        captured = time.time()
        with self._registry_lock:
            self._tabs[tab_id]["screenshot_at"] = captured
        self._emit("browser.screenshot_captured", {"captured_at": captured}, tab_id)
        return {"tab_id": tab_id, "mime": "image/jpeg", "data": result["data"], "captured_at": captured}

    def click(self, selector, tab_id=None, owner=None):
        tab_id = self._check_tab(tab_id, owner)
        action = self._begin_action(tab_id, "click", {"selector": selector})
        try:
            expression = """(() => {
                const e = document.querySelector(SELECTOR);
                if (!e) throw Error('Selector not found');
                if (e.disabled) throw Error('Element is disabled');
                e.scrollIntoView({block:'center',inline:'center'});
                const r=e.getBoundingClientRect();
                if (!r.width || !r.height) throw Error('Element is not visible');
                return {x:r.x+r.width/2,y:r.y+r.height/2};
            })()""".replace("SELECTOR", json.dumps(selector))
            result = self.rpc("Runtime.evaluate", {"expression": expression, "returnByValue": True}, tab_id, owner)
            if result.get("exceptionDetails"):
                raise BrowserError(json.dumps(result["exceptionDetails"]))
            point = result["result"]["value"]
            for kind in ("mousePressed", "mouseReleased"):
                self.rpc("Input.dispatchMouseEvent", {"type": kind, **point, "button": "left", "clickCount": 1}, tab_id, owner)
            result = {"tab_id": tab_id, "selector": selector, "status": "input_dispatched", "note": "Click dispatched; application outcome must be verified"}
            self._finish_action(tab_id, action, result)
            return result
        except Exception as error:
            self._finish_action(tab_id, action, error=error)
            raise

    def close_tab(self, tab_id, owner=None, force=False):
        tab_id = self._check_tab(tab_id, owner)
        with self._registry_lock:
            if self._tabs[tab_id].get("pinned") and not force:
                raise BrowserError("Tab is pinned; unpin it before closing")
        if not self._browser or self._browser.closed:
            raise BrowserError("Browser connection is unavailable")
        action = self._begin_action(tab_id, "close_tab", {})
        try:
            result = self._browser.call("Target.closeTarget", {"targetId": tab_id})
            if result.get("success") is False:
                raise BrowserError("Browser refused to close tab")
            with self._registry_lock:
                self._tabs[tab_id]["closed"] = True
                connection = self._pages.pop(tab_id, None)
                if self.selected == tab_id:
                    self.selected = None
                    self.ws = None
            if connection:
                connection.close()
            self._finish_action(tab_id, action, result)
            return self.tabs(owner=owner)
        except Exception as error:
            self._finish_action(tab_id, action, error=error)
            raise

    def diagnostics(self, tab_id=None, owner=None, after=0):
        tab_id = self._check_tab(tab_id, owner)
        self._ensure_page(tab_id)
        records = self.read_events(tab_id=tab_id, owner=owner, after=after, limit=5000)
        kinds = {"browser.console", "browser.exception", "browser.log", "browser.request_failed"}
        result = [record for record in records if record["type"] in kinds or record["type"] == "browser.response" and record["body"].get("status", 0) >= 400]
        with self._registry_lock:
            started = self._tabs[tab_id].get("capture_started")
        with self._event_lock:
            retained_from = self.events[0]["seq"] if self.events else self._event_sequence + 1
        return {"tab_id": tab_id, "capture_started": started, "after": after,
                "retained_from": retained_from, "history_truncated": after < retained_from - 1,
                "events": result, "next_cursor": records[-1]["seq"] if records else after,
                "coverage": "Captured events since attachment within the retained buffer; not proof that all possible errors were tested"}

    def _action_context(self, tab_id):
        with self._registry_lock:
            action = self._last_actions.get(tab_id)
            if action and time.time() - action["started"] <= 10:
                return {"action_id": action["id"], "association": "temporal_only_not_proof_of_causation"}
        return None

    def _page_event(self, tab_id, method, params):
        if method == "Network.requestWillBeSent":
            request_id = params["requestId"]
            key = (tab_id, request_id)
            previous = self._requests.get(key)
            redirect = params.get("redirectResponse")
            hop = previous["hop"] + 1 if previous and redirect else 0
            if redirect:
                self._emit("browser.redirect", {"request_id": request_id,
                    "hop": previous["hop"] if previous else 0,
                    "url": redirect.get("url"), "status": redirect.get("status"),
                    "to_url": params["request"].get("url")}, tab_id)
            request = params["request"]
            current = {"hop": hop, "started": params.get("timestamp"),
                       "url": request.get("url"), "method": request.get("method")}
            self._requests[key] = current
            body = {"request_id": request_id, "hop": hop, "url": current["url"],
                    "method": current["method"], "resource_type": params.get("type"),
                    "frame_id": params.get("frameId"), "loader_id": params.get("loaderId"),
                    "document_url": _safe_url(params.get("documentURL", "")),
                    "initiator_type": params.get("initiator", {}).get("type"),
                    "browser_timestamp": params.get("timestamp"),
                    "action_context": self._action_context(tab_id)}
            if self.capture_headers:
                body["headers"] = request.get("headers", {})
            if self.capture_post_data and "postData" in request:
                try:
                    body["post_data"] = _redact(json.loads(request["postData"]))
                except (ValueError, TypeError):
                    body["post_data"] = "[non-JSON request body omitted]"
            self._emit("browser.request", body, tab_id)
        elif method == "Network.responseReceived":
            request_id = params["requestId"]
            current = self._requests.get((tab_id, request_id), {})
            response = params.get("response", {})
            body = {"request_id": request_id, "hop": current.get("hop", 0),
                    "url": response.get("url"), "method": current.get("method"),
                    "status": response.get("status"), "mime_type": response.get("mimeType"),
                    "resource_type": params.get("type"), "protocol": response.get("protocol"),
                    "from_disk_cache": response.get("fromDiskCache", False),
                    "from_service_worker": response.get("fromServiceWorker", False)}
            if self.capture_headers:
                body["headers"] = response.get("headers", {})
            self._emit("browser.response", body, tab_id)
        elif method in ("Network.loadingFinished", "Network.loadingFailed"):
            request_id = params["requestId"]
            current = self._requests.pop((tab_id, request_id), {})
            start = current.get("started")
            end = params.get("timestamp")
            self._emit("browser.request_failed" if method.endswith("Failed") else "browser.request_finished", {
                "request_id": request_id, "hop": current.get("hop", 0),
                "url": current.get("url"), "method": current.get("method"),
                "duration_ms": max(0, (end - start) * 1000) if start is not None and end is not None else None,
                "encoded_bytes": params.get("encodedDataLength"), "error": params.get("errorText"),
                "cancelled": params.get("canceled", False), "blocked_reason": params.get("blockedReason"),
            }, tab_id)
        elif method == "Runtime.consoleAPICalled":
            arguments = []
            for value in params.get("args", []):
                arguments.append(value.get("value", value.get("description", value.get("type", ""))))
            self._emit("browser.console", {"level": params.get("type"), "arguments": arguments,
                "browser_timestamp": params.get("timestamp")}, tab_id)
        elif method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails", {})
            self._emit("browser.exception", {"message": detail.get("exception", {}).get("description", detail.get("text")),
                "url": detail.get("url"), "line": detail.get("lineNumber"),
                "column": detail.get("columnNumber"), "exception_id": detail.get("exceptionId")}, tab_id)
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            self._emit("browser.log", {"level": entry.get("level"), "source": entry.get("source"),
                "message": entry.get("text"), "url": entry.get("url"), "line": entry.get("lineNumber")}, tab_id)
        elif method == "Page.frameNavigated":
            frame = params.get("frame", {})
            if not frame.get("parentId"):
                with self._registry_lock:
                    if tab_id in self._tabs:
                        self._tabs[tab_id]["url"] = frame.get("url", "")
                self._emit("browser.navigated", {"url": frame.get("url"), "frame_id": frame.get("id")}, tab_id)
        elif method == "Page.loadEventFired":
            self._emit("browser.page_loaded", {"browser_timestamp": params.get("timestamp"), "note": "Load event is not proof that the application has no errors"}, tab_id)
        if (method in ("Runtime.exceptionThrown", "Network.loadingFailed")
                or method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "assert")
                or method == "Log.entryAdded" and params.get("entry", {}).get("level") == "error"
                or method == "Network.responseReceived" and params.get("response", {}).get("status", 0) >= 400):
            with self._registry_lock:
                if tab_id in self._tabs:
                    self._tabs[tab_id]["error_count"] += 1

    def stop(self):
        with self.lock:
            with self._registry_lock:
                connections = list(self._pages.values())
                self._pages.clear()
                for tab in self._tabs.values():
                    if not tab.get("closed"):
                        tab["closed"] = True
                self.selected = None
                self.ws = None
            for connection in connections:
                connection.close()
            if self._browser:
                self._browser.close()
                self._browser = None
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self._emit("browser.stopped", {"note": "Only the application-owned process was stopped"})
            self.process = None
            self.port = None
            self._requests.clear()
            self._last_actions.clear()
