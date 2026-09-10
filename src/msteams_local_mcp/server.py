"""MCP server exposing the local Microsoft Teams (v2) cache plus local desktop writes.

Reads come from the local Teams IndexedDB cache. Optional Windows writes drive the
already signed-in Teams desktop client via Windows UI Automation; no Microsoft Graph,
OAuth, Azure app registration, or Teams token scraping is used.
"""
from __future__ import annotations

import dataclasses
import glob
import json
import os
import pathlib
import re
import time
from collections import Counter
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP as _MCPServer  # mcp < 2
except ImportError:  # pragma: no cover
    from mcp.server.mcpserver import MCPServer as _MCPServer  # mcp >= 2

from mcp.types import ToolAnnotations

from .reader import Message, TeamsCacheReader, find_cache
from .writer import (
    LocalTeamsWriteError,
    local_write_status as _local_write_status,
    send_chat_message as _send_chat_message,
)

mcp = _MCPServer("msteams-local")

_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_WRITE_SEND = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

_LEVELDB = os.environ.get("MSTEAMS_LEVELDB") or None
_TTL = float(os.environ.get("MSTEAMS_CACHE_TTL", "180"))
_MAXLEN = int(os.environ.get("MSTEAMS_MAX_CONTENT", "600"))
_CACHE_FILE = pathlib.Path(
    os.environ.get("MSTEAMS_CACHE_DIR")
    or (pathlib.Path.home() / ".cache" / "msteams-local-mcp")
) / "snapshot.json"

_snap: dict = {
    "ts": 0.0,
    "sig": "",
    "messages": [],
    "conversations": [],
    "accounts": [],
    "mentions": [],
    "skipped": 0,
}


def _signature(leveldb: str) -> str:
    files = glob.glob(os.path.join(leveldb, "*"))
    if not files:
        return ""
    newest = max(os.path.getmtime(f) for f in files)
    total = sum(os.path.getsize(f) for f in files)
    return f"{newest:.0f}:{total}"


def _load_disk(sig: str) -> Optional[dict]:
    try:
        data = json.loads(_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return None
    if data.get("sig") != sig:
        return None
    data["messages"] = [Message(**m) for m in data.get("messages", [])]
    return data


def _save_disk(snap: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sig": snap["sig"],
            "messages": [dataclasses.asdict(m) for m in snap["messages"]],
            "conversations": snap["conversations"],
            "accounts": snap["accounts"],
            "mentions": snap["mentions"],
            "skipped": snap["skipped"],
        }
        tmp = _CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, _CACHE_FILE)
        os.chmod(_CACHE_FILE, 0o600)
    except OSError:
        pass


def _epoch_ms(ts: str) -> Optional[float]:
    if not ts:
        return None
    try:
        return float(ts)
    except (TypeError, ValueError):
        try:
            from datetime import datetime

            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
        except Exception:
            return None


def _snapshot() -> dict:
    now = time.monotonic()
    leveldb = _LEVELDB or find_cache()
    sig = _signature(leveldb) if leveldb else ""

    if _snap["messages"] and now - _snap["ts"] < _TTL and _snap["sig"] == sig:
        return _snap

    disk = _load_disk(sig) if sig else None
    if disk:
        _snap.update(ts=now, **disk)
        return _snap

    with TeamsCacheReader(leveldb) as r:
        messages = list(r.messages())
        conversations = list(r.conversations())
        mentions = list(r.mentions())
        skipped = r.skipped

    per_acc: dict[str, Counter] = {}
    for m in messages:
        c = per_acc.setdefault(m.account, Counter())
        mm = re.search(r"\(([^)]+)\)\s*$", m.sender)
        if mm:
            c[mm.group(1)] += 1
    accounts = [
        {
            "key": k,
            "tenant_id": k.split(":")[0] if ":" in k else k,
            "user_id": k.split(":")[1] if ":" in k else "",
            "label": (c.most_common(1)[0][0] if c else ""),
        }
        for k, c in per_acc.items()
    ]
    _snap.update(
        ts=now,
        sig=sig,
        messages=messages,
        conversations=conversations,
        accounts=accounts,
        mentions=mentions,
        skipped=skipped,
    )
    _save_disk(_snap)
    return _snap


def _msg_index(snap: dict) -> dict:
    return {(m.account, m.message_id): m for m in snap["messages"]}


def _title_map(snap: dict) -> dict:
    return {(c["account"], c["id"]): c.get("title", "") for c in snap["conversations"]}


def _fmt(m: Message) -> dict:
    content = m.content if len(m.content) <= _MAXLEN else m.content[:_MAXLEN] + "…"
    return {
        "sender": m.sender,
        "timestamp": m.timestamp,
        "content": content,
        "conversation_id": m.conversation_id,
        "account": m.account,
    }


@mcp.tool(annotations=_READ)
def list_accounts() -> list[dict]:
    """List the Teams accounts (tenant/user contexts) in the local cache."""
    return _snapshot()["accounts"]


@mcp.tool(annotations=_READ)
def list_conversations(account: Optional[str] = None, limit: int = 100) -> list[dict]:
    """List chats/channels (id, title, type), optionally filtered by account."""
    out = []
    for conv in _snapshot()["conversations"]:
        if account and conv.get("account") != account:
            continue
        if conv.get("title") or conv.get("last_message"):
            out.append(conv)
        if len(out) >= limit:
            break
    return out


@mcp.tool(annotations=_READ)
def read_conversation(
    conversation_id: str,
    limit: int = 50,
    account: Optional[str] = None,
) -> list[dict]:
    """Return up to limit most recent messages of a conversation (newest last)."""
    msgs = [
        m
        for m in _snapshot()["messages"]
        if m.conversation_id == conversation_id and (not account or m.account == account)
    ]
    msgs.sort(key=lambda m: _epoch_ms(m.timestamp) or 0)
    return [_fmt(m) for m in msgs[-limit:]]


@mcp.tool(annotations=_READ)
def search_messages(
    query: str,
    account: Optional[str] = None,
    days: Optional[int] = None,
    limit: int = 50,
) -> list[dict]:
    """Case-insensitive substring search across cached messages."""
    q = query.lower()
    cutoff = (time.time() - days * 86400) * 1000 if days else None
    hits = []
    for m in _snapshot()["messages"]:
        if account and m.account != account:
            continue
        if cutoff is not None:
            e = _epoch_ms(m.timestamp)
            if e is None or e < cutoff:
                continue
        if q in m.content.lower() or q in m.sender.lower():
            hits.append(_fmt(m))
            if len(hits) >= limit:
                break
    return hits


@mcp.tool(annotations=_READ)
def recent_messages(
    days: int = 7,
    account: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Messages received in the last days days, newest first."""
    cutoff = (time.time() - days * 86400) * 1000
    hits = []
    for m in _snapshot()["messages"]:
        if account and m.account != account:
            continue
        e = _epoch_ms(m.timestamp)
        if e is None or e < cutoff:
            continue
        hits.append((e, m))
    hits.sort(key=lambda t: t[0], reverse=True)
    return [_fmt(m) for _e, m in hits[:limit]]


def _mentions_impl(days, unread_only, account, limit):
    snap = _snapshot()
    idx = _msg_index(snap)
    titles = _title_map(snap)
    cutoff = (time.time() - days * 86400) * 1000 if days else None
    out = []
    for mt in snap["mentions"]:
        if account and mt["account"] != account:
            continue
        if unread_only and mt["is_read"]:
            continue
        e = _epoch_ms(mt["timestamp"])
        if cutoff is not None and (e is None or e < cutoff):
            continue
        m = idx.get((mt["account"], mt["message_id"]))
        out.append(
            {
                "timestamp": mt["timestamp"],
                "is_read": mt["is_read"],
                "conversation": titles.get(
                    (mt["account"], mt["conversation_id"]), ""
                )
                or mt["conversation_id"],
                "sender": m.sender if m else "",
                "content": (m.content[:_MAXLEN] if m else ""),
                "account": mt["account"],
            }
        )
    out.sort(key=lambda d: _epoch_ms(d["timestamp"]) or 0, reverse=True)
    return out[:limit]


@mcp.tool(annotations=_READ)
def mentions(
    days: Optional[int] = None,
    unread_only: bool = False,
    account: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """@-mentions of you, newest first, with content and mention read state."""
    return _mentions_impl(days, unread_only, account, limit)


@mcp.tool(annotations=_READ)
def unread_messages(
    days: int = 30,
    account: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Unread items needing attention: reliable unread @-mentions."""
    return _mentions_impl(days, True, account, limit)


@mcp.tool(annotations=_READ)
def overview(days: int = 7, account: Optional[str] = None) -> list[dict]:
    """Recent Teams activity grouped by conversation, newest first."""
    snap = _snapshot()
    titles = _title_map(snap)
    cutoff = (time.time() - days * 86400) * 1000
    convs: dict = {}
    for m in snap["messages"]:
        if account and m.account != account:
            continue
        e = _epoch_ms(m.timestamp)
        if e is None or e < cutoff:
            continue
        g = convs.setdefault(
            (m.account, m.conversation_id),
            {"count": 0, "senders": set(), "last_e": 0.0, "last": None},
        )
        g["count"] += 1
        if m.sender:
            g["senders"].add(m.sender)
        if e > g["last_e"]:
            g["last_e"], g["last"] = e, m
    rows = []
    for (acc, cid), g in convs.items():
        last = g["last"]
        rows.append(
            {
                "conversation": titles.get((acc, cid), "") or cid,
                "conversation_id": cid,
                "count": g["count"],
                "senders": sorted(g["senders"])[:8],
                "last_sender": last.sender if last else "",
                "last_message": (last.content[:200] if last else ""),
                "last_time": last.timestamp if last else "",
                "account": acc,
            }
        )
    rows.sort(key=lambda r: _epoch_ms(r["last_time"]) or 0, reverse=True)
    return rows


@mcp.tool(annotations=_READ, title="Check local Teams send readiness")
def local_write_status() -> dict:
    """Check whether Windows UI Automation can currently reach the local Teams client."""
    return _local_write_status()


@mcp.tool(annotations=_WRITE_SEND, title="Send Teams chat message locally")
def send_chat_message_local(conversation_id: str, message: str) -> dict:
    """Send a plain-text message to an existing Teams 1:1/group chat via the local desktop client.

    This is a WRITE action. It sends a real Teams message as the currently signed-in local Teams
    user. Use a conversation_id returned by this MCP's read tools. No Graph or OAuth is used.
    Calling twice can send duplicates, so this tool is explicitly non-idempotent.
    """
    try:
        return _send_chat_message(conversation_id, message)
    except LocalTeamsWriteError as exc:
        raise RuntimeError(str(exc)) from exc


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
