"""Command-line access to the local Teams cache (no MCP needed).

Examples:
    msteams-local-dump accounts
    msteams-local-dump search "budget" --limit 20
    msteams-local-dump conversations --account <tenant:user>
"""
from __future__ import annotations

import argparse
import json
import sys

from .reader import TeamsCacheReader


def main(argv: list[str] | None = None) -> int:
    # Common options live on a parent parser so they work both before AND after
    # the subcommand (argparse otherwise rejects globals placed after it).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--leveldb", help="path to the IndexedDB .leveldb dir (else auto-discovered)")
    common.add_argument("--account", help="filter on a <tenantId:userId> account key")
    common.add_argument("--limit", type=int, default=50)

    ap = argparse.ArgumentParser(prog="msteams-local-dump", description=__doc__, parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("accounts", parents=[common], help="list the tenant/user contexts in the cache")
    sub.add_parser("conversations", parents=[common], help="list chats/channels")
    sp = sub.add_parser("search", parents=[common], help="substring search across messages")
    sp.add_argument("query")
    args = ap.parse_args(argv)

    with TeamsCacheReader(args.leveldb) as r:
        if args.cmd == "accounts":
            out = [vars(a) | {"key": a.key} for a in r.accounts()]
        elif args.cmd == "conversations":
            out = []
            for c in r.conversations(account=args.account):
                if c.get("title") or c.get("last_message"):
                    out.append(c)
                if len(out) >= args.limit:
                    break
        elif args.cmd == "search":
            q = args.query.lower()
            out = []
            for m in r.messages(account=args.account):
                if q in m.content.lower() or q in m.sender.lower():
                    out.append(vars(m))
                    if len(out) >= args.limit:
                        break
        else:  # pragma: no cover
            ap.error("unknown command")
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        print()
        print(f"# {len(out)} result(s), {r.skipped} record(s) skipped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
