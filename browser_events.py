# browser_events.py
"""Durable, workspace-scoped browser timelines.

Browser.set_event_sink(events.record) connects browser capture to this store.
Call bind_tab() after assigning tab ownership. Runtime/API wiring is separate.
A recorded run ID is owner/context metadata, not proof of request causation.
"""
import hashlib
import json
import re
import threading
import time
import uuid


class BrowserEventError(ValueError):
    pass


GLOBAL_EVENTS = {"browser.connected", "browser.started", "browser.stopped"}
NETWORK_EVENTS = {
    "browser.request", "browser.response", "browser.redirect",
    "browser.request_finished", "browser.request_failed",
}
CONSOLE_EVENTS = {"browser.console", "browser.exception", "browser.log"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _category(event_type):
    if event_type in NETWORK_EVENTS:
        return "network"
    if event_type in CONSOLE_EVENTS:
        return "console"
    if event_type.startswith("browser.action_"):
        return "actions"
    return "lifecycle"


def _is_error(event_type, body):
    return bool(
        event_type in {"browser.exception", "browser.request_failed", "browser.action_failed", "browser.persistence_error", "browser.capture_unavailable"}
        or event_type in {"browser.console", "browser.log"} and body.get("level") in {"error", "assert"}
        or event_type == "browser.response" and isinstance(body.get("status"), (int, float)) and body["status"] >= 400
    )


class BrowserEventStore:
    def __init__(self, store, max_event_bytes=262144):
        self.s = store
        self.max_event_bytes = max(8192, int(max_event_bytes))
        self._lock = threading.RLock()
        self._plans = {}
        with self.s.db() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS browser_sessions (
                    id TEXT PRIMARY KEY,
                    created REAL NOT NULL,
                    stopped REAL
                );
                CREATE TABLE IF NOT EXISTS browser_session_scopes (
                    session_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    PRIMARY KEY(session_id, workspace)
                );
                CREATE TABLE IF NOT EXISTS browser_tab_registry (
                    session_id TEXT NOT NULL,
                    tab_id TEXT NOT NULL,
                    workspace TEXT,
                    conversation TEXT,
                    owner_run TEXT,
                    owner_agent TEXT,
                    label TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL,
                    closed REAL,
                    capture_started REAL,
                    PRIMARY KEY(session_id, tab_id)
                );
                CREATE TABLE IF NOT EXISTS browser_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    session_id TEXT NOT NULL,
                    tab_id TEXT,
                    workspace TEXT,
                    conversation TEXT,
                    owner_run TEXT,
                    owner_agent TEXT,
                    browser_seq INTEGER,
                    tab_seq INTEGER,
                    event_type TEXT NOT NULL,
                    category TEXT NOT NULL,
                    method TEXT,
                    status INTEGER,
                    url TEXT,
                    is_error INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL,
                    body TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS browser_events_scope_seq ON browser_events(workspace, seq);
                CREATE INDEX IF NOT EXISTS browser_events_tab_seq ON browser_events(session_id, tab_id, seq);
                CREATE INDEX IF NOT EXISTS browser_events_type_seq ON browser_events(event_type, seq);
            """)

    def _workspace(self, workspace):
        if not isinstance(workspace, str) or not workspace:
            raise BrowserEventError("Workspace ID required")
        self.s.get("workspace", workspace)
        return workspace

    def _owner(self, owner):
        if not owner:
            return None
        if not isinstance(owner, dict):
            raise BrowserEventError("Owner must be an object")
        self._workspace(owner.get("workspace"))
        return {k: owner.get(k) for k in ("workspace", "conversation", "run", "agent")}

    def _bind(self, c, tab):
        session_id = str(tab.get("session_id") or "")
        tab_id = str(tab.get("id") or tab.get("tab_id") or "")
        if not session_id or not tab_id:
            raise BrowserEventError("Session and tab IDs are required")
        owner = self._owner(tab.get("owner"))
        old = c.execute("SELECT * FROM browser_tab_registry WHERE session_id=? AND tab_id=?", (session_id, tab_id)).fetchone()
        if old and old["workspace"] and owner:
            same_workspace = old["workspace"] == owner["workspace"]
            same_conversation = old["conversation"] == owner.get("conversation")
            if not same_workspace or not same_conversation:
                raise BrowserEventError("Cannot reassign a tab to another workspace or conversation")
        if owner is None and old and old["workspace"]:
            owner = {"workspace": old["workspace"], "conversation": old["conversation"], "run": old["owner_run"], "agent": old["owner_agent"]}
        owner = owner or {}
        def value(key, default=""):
            return tab.get(key, old[key] if old else default)
        created = float(value("created", time.time()))
        closed = value("closed", None)
        if type(closed) is bool:
            closed = time.time() if closed else None
        c.execute("INSERT OR IGNORE INTO browser_sessions(id,created) VALUES(?,?)", (session_id, created))
        c.execute("""INSERT OR REPLACE INTO browser_tab_registry
            (session_id,tab_id,workspace,conversation,owner_run,owner_agent,label,purpose,title,url,pinned,created,closed,capture_started)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                session_id, tab_id, owner.get("workspace"), owner.get("conversation"), owner.get("run"), owner.get("agent"),
                str(value("label", "Browser tab"))[:100], str(value("purpose"))[:300], str(value("title"))[:1000],
                str(value("url"))[:4096], int(bool(value("pinned", False))), created, closed, value("capture_started", None),
            ))
        if owner.get("workspace"):
            c.execute("INSERT OR IGNORE INTO browser_session_scopes VALUES(?,?)", (session_id, owner["workspace"]))
            # Capture can begin before Target.createTarget returns its new ID.
            c.execute("""UPDATE browser_events SET workspace=?,conversation=?,owner_run=?,owner_agent=?
                WHERE session_id=? AND tab_id=? AND workspace IS NULL""", (
                    owner["workspace"], owner.get("conversation"), owner.get("run"), owner.get("agent"), session_id, tab_id,
                ))
        return {"session_id": session_id, "tab_id": tab_id, "owner": owner or None}

    def bind_tab(self, tab):
        with self._lock, self.s.db() as c:
            return self._bind(c, tab)

    def record(self, event):
        if not isinstance(event, dict):
            raise BrowserEventError("Event must be an object")
        session_id = str(event.get("session_id") or "")
        event_type = str(event.get("type") or "")
        if not session_id or not event_type.startswith("browser."):
            raise BrowserEventError("Browser event requires a session and browser.* type")
        tab_id = event.get("tab_id")
        owner = self._owner(event.get("owner"))
        created = float(event.get("created", time.time()))
        body = event.get("body") or {}
        if not isinstance(body, dict):
            body = {"value": body}
        # The Browser transport applies redaction before invoking its event sink.
        encoded = _json(body)
        if len(encoded.encode("utf-8")) > self.max_event_bytes:
            body = {"capture_truncated": True, "original_bytes": len(encoded.encode("utf-8")), "preview": encoded[:8192]}
        method = body.get("method")
        status = body.get("status")
        numeric_status = int(status) if type(status) in (int, float) else None
        url = body.get("url")
        error = int(_is_error(event_type, body))
        event_id = str(event.get("event_id") or uuid.uuid4().hex)
        with self._lock, self.s.db() as c:
            c.execute("INSERT OR IGNORE INTO browser_sessions(id,created) VALUES(?,?)", (session_id, created))
            if tab_id:
                patch = {"id": tab_id, "session_id": session_id}
                if owner:
                    patch["owner"] = owner
                if event_type == "browser.tab_created":
                    patch.update(title=body.get("title", ""), url=body.get("url", ""))
                elif event_type == "browser.tab_context_changed":
                    patch.update({k: body[k] for k in ("label", "purpose", "pinned") if k in body})
                elif event_type == "browser.navigated":
                    patch["url"] = body.get("url", "")
                elif event_type == "browser.tab_closed":
                    patch["closed"] = created
                elif event_type == "browser.capture_started":
                    patch["capture_started"] = created
                binding = self._bind(c, patch)
                owner = binding["owner"]
            owner = owner or {}
            c.execute("""INSERT OR IGNORE INTO browser_events
                (event_id,session_id,tab_id,workspace,conversation,owner_run,owner_agent,browser_seq,tab_seq,event_type,category,method,status,url,is_error,created,body)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    event_id, session_id, tab_id, owner.get("workspace"), owner.get("conversation"), owner.get("run"), owner.get("agent"),
                    event.get("seq"), event.get("tab_seq"), event_type, _category(event_type),
                    str(method).upper()[:30] if method else None, numeric_status, str(url)[:4096] if url else None,
                    error, created, _json(body),
                ))
            if event_type == "browser.stopped":
                c.execute("UPDATE browser_sessions SET stopped=? WHERE id=?", (created, session_id))
                c.execute("UPDATE browser_tab_registry SET closed=COALESCE(closed,?) WHERE session_id=?", (created, session_id))
            row = c.execute("SELECT seq FROM browser_events WHERE event_id=?", (event_id,)).fetchone()
            return row["seq"]

    def tabs(self, workspace, conversation=None, include_closed=False):
        self._workspace(workspace)
        where, args = ["workspace=?"], [workspace]
        if conversation is not None:
            where.append("conversation=?"); args.append(conversation)
        if not include_closed:
            where.append("closed IS NULL")
        with self.s.db() as c:
            rows = c.execute("SELECT * FROM browser_tab_registry WHERE " + " AND ".join(where) + " ORDER BY created", args).fetchall()
        return [dict(row) for row in rows]

    def read(self, workspace, tab_id=None, session_id=None, after=0, before=None,
             order="asc", limit=500, category=None, event_type=None, method=None,
             status=None, errors_only=False, search="", conversation=None):
        self._workspace(workspace)
        if order not in ("asc", "desc"):
            raise BrowserEventError("Order must be asc or desc")
        if category is not None and category not in ("network", "console", "actions", "lifecycle"):
            raise BrowserEventError("Unknown event category")
        # Only harmless session-level lifecycle records are shared across a session's scopes.
        where = ["(workspace=? OR (tab_id IS NULL AND event_type IN ('browser.connected','browser.started','browser.stopped') AND session_id IN (SELECT session_id FROM browser_session_scopes WHERE workspace=?)))", "seq>?"]
        args = [workspace, workspace, max(0, int(after))]
        for field, value in (("tab_id", tab_id), ("session_id", session_id), ("conversation", conversation), ("category", category), ("event_type", event_type)):
            if value is not None:
                where.append(field + "=?"); args.append(str(value))
        if before is not None:
            where.append("seq<?"); args.append(int(before))
        if method:
            method = str(method).upper()
            if not re.fullmatch(r"[A-Z-]{1,30}", method):
                raise BrowserEventError("Invalid HTTP method filter")
            where.append("method=?"); args.append(method)
        if status is not None:
            where.append("status=?"); args.append(int(status))
        if errors_only:
            where.append("is_error=1")
        if search:
            literal = str(search)[:1000].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(body LIKE ? ESCAPE '\\' OR COALESCE(url,'') LIKE ? ESCAPE '\\')")
            args.extend(["%" + literal + "%"] * 2)
        with self.s.db() as c:
            rows = c.execute("SELECT * FROM browser_events WHERE " + " AND ".join(where) + " ORDER BY seq " + order.upper() + " LIMIT ?", args + [max(1, min(int(limit), 2000))]).fetchall()
        return [{**dict(row), "body": json.loads(row["body"])} for row in rows]

    def _clear_candidates(self, workspace, cutoff, tab_id=None, session_id=None):
        self._workspace(workspace)
        where = ["e.workspace=?", "e.seq<=?", "(s.stopped IS NOT NULL OR t.closed IS NOT NULL)"]
        args = [workspace, cutoff]
        if tab_id is not None:
            where.append("e.tab_id=?"); args.append(tab_id)
        if session_id is not None:
            where.append("e.session_id=?"); args.append(session_id)
        with self.s.db() as c:
            rows = c.execute("""SELECT e.seq FROM browser_events e
                LEFT JOIN browser_sessions s ON s.id=e.session_id
                LEFT JOIN browser_tab_registry t ON t.session_id=e.session_id AND t.tab_id=e.tab_id
                WHERE """ + " AND ".join(where) + " ORDER BY e.seq", args).fetchall()
        return [row["seq"] for row in rows]

    def preview_clear(self, workspace, tab_id=None, session_id=None):
        self._workspace(workspace)
        with self._lock:
            now = time.time()
            self._plans = {k: v for k, v in self._plans.items() if now - v["created"] < 300}
            if len(self._plans) >= 100:
                raise BrowserEventError("Too many pending deletion previews")
            with self.s.db() as c:
                cutoff = c.execute("SELECT COALESCE(MAX(seq),0) AS seq FROM browser_events").fetchone()["seq"]
            ids = self._clear_candidates(workspace, cutoff, tab_id, session_id)
            token = uuid.uuid4().hex
            self._plans[token] = {"workspace": workspace, "tab_id": tab_id, "session_id": session_id, "cutoff": cutoff, "ids": ids, "hash": _hash(ids), "created": now}
            return {"token": token, "workspace": workspace, "count": len(ids), "sample": ids[:8], "expires_in": 300,
                    "note": "Deletes eligible browser events for closed tabs or stopped sessions. Open/unknown capture contexts are protected. Files, run state, tab metadata, exports and backups remain."}

    def clear(self, token, confirm=False):
        if confirm is not True:
            raise BrowserEventError("Explicit confirmation is required")
        with self._lock:
            plan = self._plans.pop(token, None)
            if not plan or time.time() - plan["created"] >= 300:
                raise BrowserEventError("Deletion preview expired")
            current = self._clear_candidates(plan["workspace"], plan["cutoff"], plan["tab_id"], plan["session_id"])
            if _hash(current) != plan["hash"]:
                raise BrowserEventError("Targets changed; preview and confirm again")
            with self.s.db() as c:
                c.execute("BEGIN IMMEDIATE")
                c.executemany("DELETE FROM browser_events WHERE seq=? AND workspace=?", [(seq, plan["workspace"]) for seq in plan["ids"]])
            result = {"workspace": plan["workspace"], "deleted": len(plan["ids"]), "note": "Browser capture history only; no page action executed or replayed"}
            self.s.log("browser_history.cleared", plan["workspace"], result)
            return result
