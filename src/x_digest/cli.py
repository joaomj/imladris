"""Command-line interface for the X bookmark archive."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from requests import RequestException

from .auth import AuthError, authorization_url, exchange_callback
from .config import load_settings
from .db import Database
from .gold import GoldStore
from .lock import LockAlreadyHeld
from .pipeline import Pipeline


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _positive_int(value: str) -> int:
    """Parse a positive integer command argument."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _print_error(error: Exception) -> int:
    """Print one machine-readable expected error to stderr."""
    print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    """Build the command parser."""
    parser = argparse.ArgumentParser(prog="x-digest")
    commands = parser.add_subparsers(dest="command", required=True)

    auth = commands.add_parser("auth", help="authorize read-only X access")
    auth.add_argument("--callback-url", help="full localhost callback URL")

    sync = commands.add_parser("sync", help="fetch bookmarks into the local vault")
    sync.add_argument(
        "--max-pages",
        type=_positive_int,
        default=None,
        help="bound bookmark pages for diagnostic testing",
    )
    sync.add_argument("--dry-run", action="store_true")

    probe = commands.add_parser("probe-post", help="fetch exactly one canonical post")
    probe.add_argument("url")
    bookmark_probe = commands.add_parser(
        "probe-bookmarks", help="fetch one ordinary post and one Article from one bookmark page"
    )
    bookmark_probe.add_argument("--max-results", type=int, default=20)

    commands.add_parser("status", help="show local vault status")
    search = commands.add_parser("search", help="search local normalized text")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show", help="show one normalized post")
    show.add_argument("post_id")

    export = commands.add_parser("export", help="export normalized posts")
    export.add_argument("--format", choices=("markdown", "json"), required=True)
    export.add_argument("--output", type=Path)
    export_post = commands.add_parser("export-post", help="export one post as Markdown")
    export_post.add_argument("post_id")
    export_post.add_argument("--output", type=Path, required=True)

    verify = commands.add_parser("verify", help="verify local archive hashes")
    verify.add_argument("--full", action="store_true")
    commands.add_parser("rebuild-silver", help="rebuild normalized records from Bronze")
    return parser


def _handle_auth(args: argparse.Namespace, settings: Any) -> int:
    if args.callback_url:
        exchange_callback(settings, args.callback_url)
        print("X authorization stored in the macOS Keychain.")
    else:
        print("Open this URL, authorize the app, then run auth again with the callback URL:")
        print(authorization_url(settings))
    return 0


def _handle_live(args: argparse.Namespace, settings: Any) -> int:
    if args.command == "sync":
        _print(Pipeline(settings).sync(args.max_pages, args.dry_run))
    elif args.command == "probe-bookmarks":
        _print(Pipeline(settings).probe_bookmarks(args.max_results))
    elif args.command == "probe-post":
        _print(Pipeline(settings).probe_post(args.url))
    return 0


def _handle_gold(args: argparse.Namespace, settings: Any) -> int:
    database = Database(settings.database_path)
    database.initialize()
    store = GoldStore(database)
    if args.command == "status":
        _print(store.status())
    elif args.command == "search":
        _print(store.search(args.query, args.limit))
    elif args.command == "show":
        result = store.show(args.post_id)
        if result is None:
            raise ValueError(f"post not found: {args.post_id}")
        _print(result)
    elif args.command == "export":
        output = args.output or settings.vault_path / "exports" / f"bookmarks.{args.format}"
        _print({"output": str(output), "posts": store.export(output, args.format)})
    elif args.command == "export-post":
        store.export_post(args.post_id, args.output)
        _print({"output": str(args.output), "post_id": args.post_id})
    elif args.command == "verify":
        result = store.verify(args.full)
        _print(result)
        return 1 if result["failed"] else 0
    else:
        _print(store.rebuild_silver(settings.vault_path / "bronze"))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one CLI command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "sync" and args.dry_run and args.max_pages is None:
        parser.error("sync --dry-run requires --max-pages for a bounded API test")
    try:
        settings = load_settings()
        if args.command == "auth":
            return _handle_auth(args, settings)
        if args.command in {"sync", "probe-post", "probe-bookmarks"}:
            return _handle_live(args, settings)
        return _handle_gold(args, settings)
    except (
        AuthError,
        LockAlreadyHeld,
        OSError,
        RequestException,
        sqlite3.Error,
        ValidationError,
        ValueError,
    ) as error:
        return _print_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
