"""MCP server exposing the local Microsoft Teams (v2) message cache read-only.

Runs over stdio. Tools:
  - ``list_accounts``      the (tenant, user) contexts present in the cache
  - ``list_conversations`` chats/channels, optionally per account
  - ``read_conversation``  messages of one conversation, newest last
  - ``search_messages``    substring search across all cached messages

All data is read from the local disk only (no Graph, no network). Set
``MSTEAMS_LEVELDB`` to point at a specific LevelDB directory; otherwise the cache
is auto-discovered for the current OS.
"""
from __future__ import annotations

import os
from typing import Optional

# The high-level server class was FastMCP in the mcp SDK 1.x and was renamed
# MCPServer in 2.0 (same API: name arg, .tool() decorator, .run() defaulting to
# stdio). Support both so the package works regardless of the installed SDK.
try:
    from mcp.server.fastmcp import FastMCP as _MCPServer  # mcp < 2
except ImportError:  # pragma: no cover
    from mcp.server.mcpserver import MCPServer as _MCPServer  # mcp >= 2

from .reader import TeamsCacheReader

mcp = _MCPServer("msteams-local")

_LEVELDB = os.environ.get("MSTEAMS_LEVELDB") or None


def _reader() -> TeamsCacheReader:
    # A fresh cold copy per call keeps results current while Teams runs. The copy
    # is a few tens of MB; fine for interactive use. Callers that need speed can
    # point MSTEAMS_LEVELDB at a pre-made snapshot.
    return TeamsCacheReader(_LEVELDB)


@mcp.tool()
def list_accounts() -> list[dict]:
    """List the Teams accounts (tenant/user contexts) found in the local cache.

    Returns for each: ``key`` (use it as the ``account`` filter elsewhere),
    ``tenant_id``, ``user_id`` and a best-effort ``label`` inferred from the
    org/display names in the messages (e.g. the company shown in ``Name (GP X)``).
    """
    with _reader() as r:
        return [
            {"key": a.key, "tenant_id": a.tenant_id, "user_id": a.user_id, "label": a.label}
            for a in r.accounts()
        ]


@mcp.tool()
def list_conversations(account: Optional[str] = None, limit: int = 100) -> list[dict]:
    """List chats/channels (id, title, type), optionally filtered by ``account``."""
    with _reader() as r:
        out = []
        for conv in r.conversations(account=account):
            if conv.get("title") or conv.get("last_message"):
                out.append(conv)
            if len(out) >= limit:
                break
        return out


@mcp.tool()
def read_conversation(conversation_id: str, limit: int = 50, account: Optional[str] = None) -> list[dict]:
    """Return up to ``limit`` most recent messages of a conversation.

    Each message: ``sender``, ``timestamp``, ``content`` (plain text), ``account``.
    """
    with _reader() as r:
        msgs = [
            m
            for m in r.messages(account=account)
            if m.conversation_id == conversation_id
        ]
    msgs.sort(key=lambda m: m.timestamp)
    return [
        {"sender": m.sender, "timestamp": m.timestamp, "content": m.content, "account": m.account}
        for m in msgs[-limit:]
    ]


@mcp.tool()
def search_messages(query: str, account: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Case-insensitive substring search across all cached messages."""
    q = query.lower()
    hits = []
    with _reader() as r:
        for m in r.messages(account=account):
            if q in m.content.lower() or q in m.sender.lower():
                hits.append(
                    {
                        "sender": m.sender,
                        "timestamp": m.timestamp,
                        "content": m.content,
                        "conversation_id": m.conversation_id,
                        "account": m.account,
                    }
                )
                if len(hits) >= limit:
                    break
    return hits


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
