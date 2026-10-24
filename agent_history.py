#!/usr/bin/env python3
"""agent-history: keep and search every Claude Code and Codex conversation on this machine.

Claude Code deletes session transcripts after 30 days by default. This tool
copies each transcript's exact bytes into a private store before that happens,
and builds a full-text index you can search from the terminal. Secrets are
redacted in the index and in every result. Source files are only ever read.

Standard library only. Python 3.9+ with SQLite FTS5 (the default in python.org,
Homebrew, and Linux distribution builds).
"""

import argparse
import datetime as _dt
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

REDACTED = "[REDACTED]"
TOOL_TEXT_LIMIT = 20000  # tool output beyond this stays in the raw snapshot, not the index
_SECRET = re.compile(
    r"(?:(?<![A-Za-z0-9])sk-(?:ant-|proj-|svcacct-|live_|test_)?[A-Za-z0-9_-]{16,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bAKIA[A-Z0-9]{16}\b|\bAIza[A-Za-z0-9_-]{30,}|\bxox[baprse]-[A-Za-z0-9-]{10,}"
    r"|\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}|\bwhsec_[A-Za-z0-9]{10,}"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"
    r"|-----BEGIN [\w ]*PRIVATE KEY-----.*?-----END [\w ]*PRIVATE KEY-----)",
    re.S,
)
_ASSIGNMENT = re.compile(
    r"(?i)([\"']?\b(?:password|passwd|pwd|api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|secret[_-]?key|private[_-]?key|authorization|cookie|session[_-]?token)\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s,;}\"']{4,})"
)
_URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)", re.I)
_SIGNED_URL = re.compile(r"https?://[^\s<>\"']*[?&](?:token|key|signature|sig|X-Amz-Signature|access_token)=[^\s<>\"']+", re.I)


def redact(text):
    """Remove anything that looks like a credential. Over-redaction is deliberate."""
    if not text:
        return text
    text = _SECRET.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(r"\1" + REDACTED + r"\2", text)
    text = _ASSIGNMENT.sub(r"\1" + REDACTED, text)
    return _SIGNED_URL.sub("[REDACTED_URL]", text)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def store_dir():
    return Path(os.environ.get("AGENT_HISTORY_HOME") or Path.home() / ".agent-history").expanduser()


def default_sources():
    """(tool, path) for every transcript on this machine. Missing roots are skipped."""
    home = Path.home()
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser()
    codex = Path(os.environ.get("CODEX_HOME") or home / ".codex").expanduser()
    roots = [
        ("claude", claude / "projects"),
        ("codex", codex / "sessions"),
        ("codex", codex / "archived_sessions"),
        ("claude", home / "Library/Application Support/Claude/local-agent-mode-sessions"),
    ]
    for tool, root in roots:
        if root.is_dir():
            for path in sorted(root.rglob("*.jsonl")):
                yield tool, path
    for tool, path in (("claude-prompts", claude / "history.jsonl"), ("codex-prompts", codex / "history.jsonl")):
        if path.is_file():
            yield tool, path


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sources(
  path TEXT PRIMARY KEY, tool TEXT, size INTEGER, mtime_ns INTEGER, sha256 TEXT,
  snapshot TEXT, status TEXT, records INTEGER, messages INTEGER, errors INTEGER,
  indexed_at REAL, missing INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS snapshots(
  path TEXT, sha256 TEXT, size INTEGER, file TEXT, taken_at REAL, PRIMARY KEY(path, sha256));
CREATE TABLE IF NOT EXISTS texts(id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE, body TEXT);
CREATE TABLE IF NOT EXISTS messages(
  source TEXT, line INTEGER, seq INTEGER, session TEXT, tool TEXT, role TEXT, channel TEXT,
  ts TEXT, epoch REAL, project TEXT, text_id INTEGER, PRIMARY KEY(source, line, seq));
CREATE INDEX IF NOT EXISTS messages_session ON messages(session, epoch, line, seq);
CREATE INDEX IF NOT EXISTS messages_text ON messages(text_id);
CREATE TABLE IF NOT EXISTS sessions(
  session TEXT PRIMARY KEY, tool TEXT, project TEXT, title TEXT, source TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(body, content='texts', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS texts_ai AFTER INSERT ON texts BEGIN
  INSERT INTO search(rowid, body) VALUES (new.id, new.body);
END;
"""


def open_store(create=True):
    root = store_dir()
    if not create and not (root / "history.sqlite").exists():
        raise SystemExit(f"agent-history: no index at {root}. Run: agent-history index")
    old = os.umask(0o077)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / "snapshots").mkdir(mode=0o700, exist_ok=True)
        root.chmod(0o700)
        conn = sqlite3.connect(root / "history.sqlite")
        try:
            conn.executescript(SCHEMA)
        except sqlite3.OperationalError as e:
            if "fts5" in str(e).lower():
                raise SystemExit("agent-history: this Python's SQLite was built without FTS5. Use a python.org, Homebrew or distribution Python.")
            raise
        return conn
    finally:
        os.umask(old)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# Text the tools inject into the user's turn (instructions, environment, reminders).
# It is indexed as role "system", which search hides unless asked.
_INJECTED = re.compile(
    r"^\s*(?:# AGENTS\.md instructions|<INSTRUCTIONS>|<environment_context>|<user_instructions>|<permissions instructions>"
    r"|<system-reminder>|<local-command-caveat>|Caveat: The messages below were generated|The following is the Codex)"
)

_SKIP_BLOCKS = {"image", "image_url", "input_image", "audio", "thinking", "redacted_thinking", "reasoning", "document"}


def text_of(content):
    """Visible text from the many content shapes the tools write. Thinking and media are skipped."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(t for t in (text_of(c) for c in content) if t)
    if isinstance(content, dict):
        if content.get("type") in _SKIP_BLOCKS:
            return ""
        for key in ("text", "content", "output", "parts"):
            if key in content:
                return text_of(content[key])
    return ""


def epoch_of(value):
    """Seconds since 1970 from ISO strings, epoch seconds or epoch milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000 if v > 1e11 else v
    s = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return epoch_of(float(s))
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _tool_call_text(name, args):
    if isinstance(args, (dict, list)):
        args = json.dumps(args, ensure_ascii=False)
    args = str(args or "")
    return f"{name}: {args[:4000]}" if name else args[:4000]


def parse_record(row, tool, state):
    """Return [(role, channel, text)] for one JSONL record and update per-file state."""
    out = []
    typ = row.get("type", "")
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    for key in ("sessionId", "session_id"):
        if row.get(key):
            state["session"] = str(row[key])
    if row.get("cwd"):
        state["project"] = row["cwd"]

    if tool.endswith("-prompts"):
        if row.get("sessionId") or row.get("session_id"):
            state["session"] = str(row.get("sessionId") or row.get("session_id"))
        if row.get("project"):
            state["project"] = row["project"]
        text = row.get("display") or row.get("text") or ""
        return [("user", "prompt-history", text)] if text else []

    # Claude Code
    if typ == "custom-title" and row.get("customTitle"):
        state["title"] = row["customTitle"]
        return []
    if typ in ("user", "assistant") and isinstance(row.get("message"), dict):
        msg = row["message"]
        role = msg.get("role") or typ
        blocks = msg.get("content")
        if isinstance(blocks, list):
            said, results = [], []
            for b in blocks:
                if not isinstance(b, dict):
                    said.append(text_of(b))
                elif b.get("type") == "tool_result":
                    results.append(text_of(b.get("content")))
                elif b.get("type") == "tool_use":
                    out.append(("tool", "tool_use", _tool_call_text(b.get("name"), b.get("input"))))
                else:
                    said.append(text_of(b))
            if any(said):
                said_text = "\n".join(t for t in said if t)
                injected = role == "user" and (row.get("isMeta") or _INJECTED.match(said_text))
                out.insert(0, ("system" if injected else role, "injected" if injected else "", said_text))
            if any(results):
                out.append(("tool", "tool_result", "\n".join(t for t in results if t)))
        else:
            text = text_of(blocks)
            injected = role == "user" and (row.get("isMeta") or _INJECTED.match(text or ""))
            out.append(("system" if injected else role, "injected" if injected else "", text))
        if not state.get("first_prompt") and out and out[0][0] == "user" and not out[0][2].lstrip().startswith("<"):
            state["first_prompt"] = out[0][2]
        return out

    # Codex
    if typ == "session_meta":
        state["session"] = str(payload.get("id") or payload.get("session_id") or state["session"])
        if payload.get("cwd"):
            state["project"] = payload["cwd"]
        return []
    if typ == "turn_context" and payload.get("cwd"):
        state["project"] = payload["cwd"]
        return []
    if typ == "response_item":
        ptype = payload.get("type")
        if ptype == "message":
            role = payload.get("role", "unknown")
            text = text_of(payload.get("content"))
            if _paired(state, "response", role, text):
                return []
            if role == "user" and _INJECTED.match(text or ""):
                return [("system", "injected", text)]
            if role == "user" and not state.get("first_prompt") and not text.lstrip().startswith("<"):
                state["first_prompt"] = text
            return [(role, "", text)]
        if ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            return [("tool", "tool_use", _tool_call_text(payload.get("name"), payload.get("arguments") or payload.get("input") or payload.get("action")))]
        if ptype in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            return [("tool", "tool_result", text_of(payload.get("output")))]
        return []
    if typ == "event_msg" and payload.get("type") in ("user_message", "agent_message"):
        role = "user" if payload["type"] == "user_message" else "assistant"
        text = text_of(payload.get("message"))
        if _paired(state, "event", role, text):
            return []
        if role == "user" and _INJECTED.match(text or ""):
            return [("system", "injected", text)]
        if role == "user" and not state.get("first_prompt") and not text.lstrip().startswith("<"):
            state["first_prompt"] = text
        return [(role, "event", text)]
    return []


def _paired(state, kind, role, text):
    """Codex can write one message twice, as a response item and as an event.

    Pair them one to one: a copy is dropped only when the other kind already
    produced the same text and that copy has not been paired yet. A message the
    person really sent twice still appears twice.
    """
    other = "event" if kind == "response" else "response"
    key = (role, text)
    pending = state["seen"].setdefault(other, {})
    if pending.get(key):
        pending[key] -= 1
        return True
    mine = state["seen"].setdefault(kind, {})
    mine[key] = mine.get(key, 0) + 1
    return False


def _text_id(conn, body):
    digest = hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()
    conn.execute("INSERT OR IGNORE INTO texts(sha256, body) VALUES (?, ?)", (digest, body))
    return conn.execute("SELECT id FROM texts WHERE sha256=?", (digest,)).fetchone()[0]


def _snapshot(conn, root, path, data, digest):
    """Write the exact bytes; drop an older snapshot of the same path that this one extends."""
    name = hashlib.sha256(str(path).encode()).hexdigest()[:16] + "-" + digest[:12] + ".jsonl.gz"
    target = root / "snapshots" / name
    if not target.exists():
        partial = target.with_suffix(".partial")
        old = os.umask(0o077)
        try:
            with gzip.open(partial, "wb", compresslevel=3) as out:
                out.write(data)
        finally:
            os.umask(old)
        os.replace(partial, target)
    size = len(data)
    conn.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)", (str(path), digest, size, name, time.time()))
    # An append-only transcript grows; its previous snapshot is a strict prefix and adds nothing.
    for old_sha, old_size, old_file in conn.execute(
        "SELECT sha256, size, file FROM snapshots WHERE path=? AND sha256<>?", (str(path), digest)
    ).fetchall():
        if old_size < size and hashlib.sha256(data[:old_size]).hexdigest() == old_sha:
            try:
                (root / "snapshots" / old_file).unlink()
            except FileNotFoundError:
                pass
            conn.execute("DELETE FROM snapshots WHERE path=? AND sha256=?", (str(path), old_sha))
    return name


def index_source(conn, tool, path, snapshots=True, force=False):
    """Index one JSONL file. Returns 'unchanged', 'indexed' or 'changed-during-read'."""
    root = store_dir()
    st = path.stat()
    prior = conn.execute("SELECT size, mtime_ns, status FROM sources WHERE path=?", (str(path),)).fetchone()
    if not force and prior and prior[0] == st.st_size and prior[1] == st.st_mtime_ns and prior[2] == "complete":
        conn.execute("UPDATE sources SET missing=0 WHERE path=?", (str(path),))
        return "unchanged"
    data = path.read_bytes()[: st.st_size]
    digest = hashlib.sha256(data).hexdigest()
    state = {"session": path.stem, "project": "", "title": "", "first_prompt": "", "seen": {}}
    subagent = "subagents" in path.parts
    conn.execute("DELETE FROM messages WHERE source=?", (str(path),))
    records = messages = errors = 0
    sessions = {}
    for line_no, raw in enumerate(data.splitlines(), 1):
        if not raw.strip():
            continue
        records += 1
        try:
            row = json.loads(raw)
        except ValueError:
            errors += 1
            continue
        if not isinstance(row, dict):
            errors += 1
            continue
        try:
            entries = parse_record(row, tool, state)
        except (TypeError, AttributeError, KeyError):
            errors += 1
            continue
        ts = row.get("timestamp", row.get("ts", ""))
        epoch = epoch_of(ts)
        for seq, (role, channel, text) in enumerate(entries):
            if not text or not text.strip():
                continue
            if role == "tool" and len(text) > TOOL_TEXT_LIMIT:
                text = text[:TOOL_TEXT_LIMIT] + f"\n[… {len(text) - TOOL_TEXT_LIMIT} more characters in the snapshot]"
            text_id = _text_id(conn, redact(text))
            conn.execute(
                "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(path), line_no, seq, state["session"], tool.replace("-prompts", ""), role, channel, str(ts), epoch, state["project"], text_id),
            )
            messages += 1
            sessions[state["session"]] = (state["project"], state["title"], state["first_prompt"])
    if not tool.endswith("-prompts") and not subagent:
        for sid, (project, custom, first) in sessions.items():
            clean = lambda t: redact(" ".join((t or "").split()))[:160]
            # A title the person set wins; otherwise the first prompt fills an empty title.
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?) ON CONFLICT(session) DO UPDATE SET "
                "project=COALESCE(NULLIF(excluded.project,''), sessions.project), "
                "title=CASE WHEN ? THEN excluded.title "
                "WHEN sessions.source=excluded.source AND excluded.title<>'' THEN excluded.title "
                "ELSE COALESCE(NULLIF(sessions.title,''), excluded.title) END, "
                "source=excluded.source, tool=excluded.tool",
                (sid, tool, project, clean(custom or first), str(path), 1 if custom else 0),
            )
    snap = _snapshot(conn, root, path, data, digest) if snapshots else None
    after = path.stat()
    status = "complete" if (after.st_size, after.st_mtime_ns) == (st.st_size, st.st_mtime_ns) else "changed-during-read"
    conn.execute(
        "INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
        (str(path), tool, len(data), st.st_mtime_ns, digest, snap, status, records, messages, errors, time.time()),
    )
    conn.commit()
    return "indexed" if status == "complete" else status


# ---------------------------------------------------------------------------
# Official exports (Claude.ai and ChatGPT conversations.json)
# ---------------------------------------------------------------------------


def import_export(conn, path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"agent-history: {path} is not a conversations export (expected a JSON list)")
    src = str(Path(path).resolve())
    prefix = src + "#"
    conn.execute("DELETE FROM messages WHERE source=? OR substr(source,1,?)=?", (src, len(prefix), prefix))
    chats = count = 0
    for chat in data:
        if not isinstance(chat, dict):
            continue
        if "chat_messages" in chat:
            tool, sid = "claude.ai", str(chat.get("uuid"))
            title = chat.get("name") or ""
            msgs = []
            for m in chat["chat_messages"]:
                role = {"human": "user", "assistant": "assistant"}.get(m.get("sender"))
                content = m.get("content")
                text = text_of(content) if isinstance(content, list) and content else (m.get("text") or "")
                msgs.append((role, text, m.get("created_at")))
        elif "mapping" in chat:
            tool, sid = "chatgpt", str(chat.get("conversation_id") or chat.get("id"))
            title = chat.get("title") or ""
            msgs = []
            for node in _chatgpt_path(chat):
                m = node.get("message") or {}
                role = (m.get("author") or {}).get("role")
                content = m.get("content") or {}
                if content.get("content_type") not in (None, "text", "multimodal_text"):
                    continue
                parts = [p for p in content.get("parts") or [] if isinstance(p, str)]
                msgs.append(({"user": "user", "assistant": "assistant"}.get(role), "\n".join(parts), m.get("create_time")))
        else:
            continue
        chats += 1
        for ordinal, (role, text, ts) in enumerate(msgs, 1):
            if not role or not text or not text.strip():
                continue
            conn.execute(
                "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (prefix + sid, ordinal, 0, sid, tool, role, "export", str(ts or ""), epoch_of(ts), "", _text_id(conn, redact(text))),
            )
            count += 1
        conn.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)", (sid, tool, "", redact(title)[:160], src))
    raw = Path(path).read_bytes()
    conn.execute(
        "INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
        (src, "export", len(raw), Path(path).stat().st_mtime_ns, hashlib.sha256(raw).hexdigest(), None, "complete", chats, count, 0, time.time()),
    )
    conn.commit()
    return chats, count


def _chatgpt_path(chat):
    """The visible branch of a ChatGPT conversation, root to current node."""
    mapping = chat.get("mapping") or {}
    node_id = chat.get("current_node")
    path = []
    while node_id and node_id in mapping:
        path.append(mapping[node_id])
        node_id = mapping[node_id].get("parent")
    if path:
        return list(reversed(path))
    return sorted(mapping.values(), key=lambda n: ((n.get("message") or {}).get("create_time") or 0))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_HIDDEN_ROLES = ("developer", "system")


def _fmt_time(epoch):
    if not epoch:
        return "                "
    return _dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def _short_project(p):
    if not p:
        return ""
    home = str(Path.home())
    return ("~" + p[len(home):]) if p.startswith(home) else p


_BARE = re.compile(r"[\w\u0080-\uffff]+\*?")


def _normalize_fts(query):
    """Quote words FTS5 would reject (wow-ui, v1.2, foo/bar) and keep real FTS5 syntax."""
    out = []
    for m in re.finditer(r'"[^"]*"|\S+', query):
        tok = m.group(0)
        if tok.startswith('"') or tok.upper() in ("AND", "OR", "NOT") or _BARE.fullmatch(tok) or any(c in tok for c in "()^:"):
            out.append(tok)
        else:
            out.append('"' + tok.replace('"', "") + '"')
    return " ".join(out)


def _safe_fts(query):
    terms = re.findall(r"[\w\u0080-￿]+(?:[.'][\w\u0080-￿]+)*", query)
    return " ".join('"' + t.replace('"', '""') + '"' for t in terms)


def cmd_index(args):
    conn = open_store()
    found = list(default_sources())
    for extra in args.source or []:
        p = Path(extra).expanduser()
        files = sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]
        found += [(args.tool or "claude", f) for f in files]
    seen_paths = set()
    counts = {"indexed": 0, "unchanged": 0, "changed-during-read": 0, "errors": 0}
    started = time.time()
    for n, (tool, path) in enumerate(found, 1):
        seen_paths.add(str(path))
        try:
            result = index_source(conn, tool, path, snapshots=not args.no_snapshots, force=args.force)
            counts[result] += 1
        except OSError as e:
            counts["errors"] += 1
            print(f"agent-history: cannot read {path}: {e}", file=sys.stderr)
        if not args.quiet and sys.stderr.isatty() and (n % 25 == 0 or n == len(found)):
            print(f"\r  {n}/{len(found)} files", end="", file=sys.stderr, flush=True)
    if not args.quiet and sys.stderr.isatty() and found:
        print(file=sys.stderr)
    # Sources that vanished (for example, swept after 30 days) stay searchable.
    gone = 0
    for (path,) in conn.execute("SELECT path FROM sources WHERE tool<>'export'").fetchall():
        if path not in seen_paths and not Path(path).exists():
            conn.execute("UPDATE sources SET missing=1 WHERE path=?", (path,))
            gone += 1
    conn.commit()
    totals = conn.execute("SELECT count(*), coalesce(sum(messages),0) FROM sources").fetchone()
    report = {
        "files_found": len(found),
        "indexed": counts["indexed"],
        "unchanged": counts["unchanged"],
        "changed_during_read": counts["changed-during-read"],
        "unreadable": counts["errors"],
        "kept_after_source_deleted": gone,
        "sources_total": totals[0],
        "messages_total": totals[1],
        "seconds": round(time.time() - started, 1),
        "store": str(store_dir()),
    }
    shown_store = _short_project(report["store"])
    if args.json:
        print(json.dumps(report))
    else:
        print(
            f"{report['files_found']} transcript files: {report['indexed']} indexed, {report['unchanged']} unchanged"
            + (f", {report['changed_during_read']} changed while reading (run again)" if report["changed_during_read"] else "")
            + (f", {report['unreadable']} unreadable" if report["unreadable"] else "")
            + f".\n{report['messages_total']} messages from {report['sources_total']} sources"
            + (f", {gone} kept after their source was deleted" if gone else "")
            + f". Store: {shown_store} ({report['seconds']}s)"
        )
    return 0


def _filters(args, alias="m"):
    where, params = [], []
    if args.role:
        where.append(f"{alias}.role=?")
        params.append(args.role)
    elif not getattr(args, "all_roles", False):
        where.append(f"{alias}.role NOT IN ({','.join('?' * len(_HIDDEN_ROLES))})")
        params += list(_HIDDEN_ROLES)
    if getattr(args, "tool", None):
        where.append(f"{alias}.tool=?")
        params.append(args.tool)
    if getattr(args, "project", None):
        where.append(f"{alias}.project LIKE ?")
        params.append(f"%{args.project}%")
    if getattr(args, "session", None):
        where.append(f"{alias}.session LIKE ?")
        params.append(args.session + "%")
    if getattr(args, "since", None):
        e = epoch_of(args.since)
        if e is None:
            raise SystemExit(f"agent-history: --since must be a date like 2026-09-01, not {args.since!r}")
        where.append(f"{alias}.epoch>=?")
        params.append(e)
    return where, params


def cmd_search(args):
    conn = open_store(create=False)
    where, params = _filters(args)
    tty = sys.stdout.isatty() and not args.json and not os.environ.get("NO_COLOR")
    hl = ("\x1b[1;33m", "\x1b[0m") if tty else ("[", "]")
    sql = (
        "SELECT m.session, m.tool, m.role, m.channel, m.epoch, m.ts, m.project, m.source, m.line, "
        "snippet(search, 0, ?, ?, ' … ', 40) FROM search JOIN messages m ON m.text_id = search.rowid "
        "WHERE search MATCH ?" + "".join(" AND " + w for w in where) + " ORDER BY m.epoch DESC LIMIT ?"
    )

    def run(q):
        return conn.execute(sql, [hl[0], hl[1], q] + params + [args.limit * 4]).fetchall()

    try:
        rows = run(_normalize_fts(args.query))
    except sqlite3.OperationalError:
        safe = _safe_fts(args.query)
        if not safe:
            raise SystemExit("agent-history: nothing to search for")
        rows = run(safe)
    out, seen = [], set()
    for r in rows:
        key = (r[0], r[2], r[9])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= args.limit:
            break
    for session, tool, role, channel, epoch, ts, project, source, line, snip in out:
        snip = redact(" ".join(snip.split()))
        if args.json:
            print(json.dumps({"session": session, "tool": tool, "role": role, "channel": channel, "time": ts,
                              "project": project, "source": source, "line": line, "excerpt": snip}, ensure_ascii=False))
        else:
            who = role if not channel or channel in ("event", "export", "prompt-history") else f"{role}/{channel}"
            print(f"{_fmt_time(epoch)}  {tool:<9} {session[:8]}  {who:<16} {_short_project(project)}")
            print(f"    {snip}\n")
    if not out and not args.json:
        print("No matches.", file=sys.stderr)
    return 0 if out else 1


def cmd_sessions(args):
    conn = open_store(create=False)
    where, params = _filters(args)
    rows = conn.execute(
        "SELECT m.session, m.tool, max(m.epoch), min(m.epoch), "
        "CASE WHEN sum(m.channel<>'prompt-history')>0 THEN sum(m.channel<>'prompt-history') ELSE count(*) END, "
        "coalesce(nullif(s.title,''), (SELECT t.body FROM messages m2 JOIN texts t ON t.id=m2.text_id "
        "  WHERE m2.session=m.session AND m2.role='user' ORDER BY m2.epoch LIMIT 1), ''), "
        "coalesce(nullif(s.project,''), max(m.project)), max(coalesce(src.missing, 0)) "
        "FROM messages m LEFT JOIN sessions s ON s.session=m.session LEFT JOIN sources src ON src.path=m.source "
        "WHERE 1=1" + "".join(" AND " + w for w in where) +
        " GROUP BY m.session ORDER BY max(m.epoch) DESC LIMIT ?",
        params + [args.limit],
    ).fetchall()
    for sid, tool, last, first, n, title, project, missing in rows:
        if args.json:
            print(json.dumps({"session": sid, "tool": tool, "first": first, "last": last, "messages": n, "title": " ".join(title.split())[:160],
                              "project": project, "source_deleted": bool(missing)}, ensure_ascii=False))
        else:
            title = " ".join(title.split())[:160]
            kept = "  [source deleted, kept here]" if missing else ""
            print(f"{_fmt_time(last)}  {tool:<9} {sid[:8]}  {n:>5} {'msg ' if n == 1 else 'msgs'}  {_short_project(project)}{kept}")
            if title:
                print(f"    {title}")
    return 0 if rows else 1


def cmd_show(args):
    conn = open_store(create=False)
    sessions = [r[0] for r in conn.execute("SELECT DISTINCT session FROM messages WHERE session LIKE ?", (args.session + "%",))]
    if not sessions:
        raise SystemExit(f"agent-history: no session starts with {args.session!r}")
    if len(sessions) > 1:
        raise SystemExit("agent-history: that prefix matches several sessions: " + ", ".join(s[:12] for s in sessions[:8]))
    sid = sessions[0]
    args.session = sid
    where, params = _filters(args)
    base = "SELECT m.role, m.channel, m.epoch, t.body FROM messages m JOIN texts t ON t.id=m.text_id WHERE "
    order = " ORDER BY (m.source LIKE '%/subagents/%'), m.source, m.line, m.seq"
    rows = conn.execute(base + "m.channel<>'prompt-history'" + "".join(" AND " + w for w in where) + order, params).fetchall()
    if not rows:  # only the prompt history survives for this session
        rows = conn.execute(base + "1=1" + "".join(" AND " + w for w in where) + order, params).fetchall()
    shown = 0
    for role, channel, epoch, body in rows:
        if not args.tools and role == "tool":
            continue
        shown += 1
        if args.json:
            print(json.dumps({"role": role, "channel": channel, "time": epoch, "text": body}, ensure_ascii=False))
            continue
        label = role.upper() + (f" ({channel})" if channel and channel not in ("event", "export") else "")
        text = body if args.full or len(body) <= 1200 else body[:1200] + f" … [{len(body) - 1200} more characters, --full]"
        print(f"── {label}  {_fmt_time(epoch)}\n{text}\n")
    return 0 if shown else 1


def cmd_restore(args):
    conn = open_store(create=False)
    target = args.source
    row = conn.execute("SELECT path FROM sources WHERE path=?", (target,)).fetchone()
    if not row:
        hits = conn.execute(
            "SELECT DISTINCT source FROM messages WHERE session LIKE ? AND source NOT LIKE '%#%' "
            "AND source IN (SELECT path FROM sources WHERE tool NOT LIKE '%prompts')", (target + "%",)
        ).fetchall()
        main = [h for h in hits if "/subagents/" not in h[0].replace(os.sep, "/")]
        hits = main or hits
        if len(hits) != 1:
            listed = "".join(f"\n  {h[0]}" for h in hits[:10])
            raise SystemExit("agent-history: give a transcript path or a session id that matches exactly one transcript" + listed)
        target = hits[0][0]
    snap = conn.execute("SELECT file, sha256 FROM snapshots WHERE path=? ORDER BY taken_at DESC LIMIT 1", (target,)).fetchone()
    if not snap:
        raise SystemExit(f"agent-history: no snapshot of {target} (was it indexed with --no-snapshots?)")
    dest = Path(args.destination)
    digest = hashlib.sha256()
    try:
        with gzip.open(store_dir() / "snapshots" / snap[0], "rb") as src, open(dest, "xb") as out:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                digest.update(chunk)
                out.write(chunk)
    except FileExistsError:
        raise SystemExit(f"agent-history: {dest} already exists. Restore never overwrites.")
    if digest.hexdigest() != snap[1]:
        dest.unlink()
        raise SystemExit("agent-history: snapshot checksum mismatch; nothing was restored")
    print(f"Restored {_short_project(target)}\n      to {dest}\n  sha256 {snap[1]} (verified)")
    return 0


def cmd_stats(args):
    conn = open_store(create=False)
    by_tool = conn.execute(
        "SELECT tool, count(DISTINCT session), count(*), min(epoch), max(epoch) FROM messages GROUP BY tool ORDER BY count(*) DESC"
    ).fetchall()
    missing = conn.execute("SELECT count(*), coalesce(sum(messages),0) FROM sources WHERE missing=1").fetchone()
    size = sum(f.stat().st_size for f in store_dir().rglob("*") if f.is_file())
    if args.json:
        print(json.dumps({"tools": [dict(zip(("tool", "sessions", "messages", "first", "last"), r)) for r in by_tool],
                          "sources_deleted_but_kept": missing[0], "messages_kept_from_deleted_sources": missing[1],
                          "store_bytes": size, "store": str(store_dir())}))
        return 0
    for tool, sessions, n, first, last in by_tool:
        span = f"{_fmt_time(first)[:10]} to {_fmt_time(last)[:10]}" if first else ""
        print(f"{tool:<10} {sessions:>6} sessions {n:>9} messages  {span}")
    if missing[0]:
        print(f"\n{missing[0]} transcripts were deleted at the source and are kept here ({missing[1]} messages).")
    print(f"\nStore: {_short_project(str(store_dir()))} ({size / 1e6:.1f} MB)")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent-history", description="Keep and search every Claude Code and Codex conversation on this machine.")
    p.add_argument("--version", action="version", version=f"agent-history {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("index", help="snapshot and index every transcript (incremental)")
    s.add_argument("--source", action="append", help="also index this file or folder of .jsonl transcripts")
    s.add_argument("--tool", choices=["claude", "codex"], help="format of --source files (default: claude)")
    s.add_argument("--no-snapshots", action="store_true", help="index only; keep no copy of the raw bytes")
    s.add_argument("--force", action="store_true", help="re-read files even if unchanged")
    s.add_argument("--json", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_index)

    def common(sp, roles=True):
        sp.add_argument("--tool", help="claude, codex, claude.ai or chatgpt")
        sp.add_argument("--project", help="only sessions whose working directory contains this text")
        sp.add_argument("--since", help="only messages on or after this date (2026-09-01)")
        if roles:
            sp.add_argument("--role", choices=["user", "assistant", "tool", "developer", "system"])
            sp.add_argument("--all-roles", action="store_true", help="include developer and system messages")
        sp.add_argument("--json", action="store_true")

    s = sub.add_parser("search", help="full-text search (SQLite FTS5 syntax: \"exact phrase\", a OR b, deploy*)")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--session", help="only this session (id prefix)")
    common(s)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("sessions", help="list sessions, newest first")
    s.add_argument("--limit", type=int, default=20)
    common(s, roles=False)
    s.set_defaults(func=cmd_sessions, role=None, all_roles=False)

    s = sub.add_parser("show", help="print one conversation in order")
    s.add_argument("session", help="session id or a unique prefix")
    s.add_argument("--tools", action="store_true", help="include tool calls and results")
    s.add_argument("--full", action="store_true", help="do not shorten long messages")
    common(s)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("restore", help="write a transcript's exact original bytes to a new file")
    s.add_argument("source", help="original transcript path, or a session id")
    s.add_argument("destination")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("import", help="index an official Claude.ai or ChatGPT conversations.json export")
    s.add_argument("export")
    s.set_defaults(func=lambda a: _cmd_import(a))

    s = sub.add_parser("stats", help="what is in the store")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    if getattr(args, "limit", 1) < 1:
        p.error("--limit must be at least 1")
    try:
        return args.func(args)
    except BrokenPipeError:
        return 0


def _cmd_import(args):
    conn = open_store()
    chats, count = import_export(conn, args.export)
    print(f"Imported {count} messages from {chats} conversations in {args.export}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
