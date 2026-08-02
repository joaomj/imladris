"""Command-line interface for the X bookmark archive."""

import argparse
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from requests import RequestException

from .auth import AuthError, authorization_url, exchange_callback
from .config import load_settings
from .db import Database
from .gold import GoldStore
from .lock import LockAlreadyHeld
from .logging_setup import JsonlLogger
from .markdown import MarkdownWriter
from .pipeline import Pipeline


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _logger_for(settings: Any) -> JsonlLogger:
    return JsonlLogger(
        settings.log_path, settings.log_level, settings.log_max_bytes, settings.log_backups
    )


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
    parser.add_argument(
        "--log-level",
        choices=("debug", "info", "warning", "error"),
        help="override the configured log level for this command",
    )
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
    sync.add_argument(
        "--ignore-folder",
        action="append",
        default=[],
        help="skip one bookmark folder by name or ID; repeat to ignore several",
    )
    sync.add_argument(
        "--full",
        action="store_true",
        help="force a complete re-read and re-hydration of all folder content",
    )

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
    commands.add_parser(
        "markdown", help="write Markdown files for posts that do not have one yet"
    )
    return parser


def _handle_auth(args: argparse.Namespace, settings: Any, correlation_id: str) -> int:
    log = _logger_for(settings)
    if args.callback_url:
        exchange_callback(settings, args.callback_url)
        log.emit(correlation_id, "token_exchanged", "info")
        print("X authorization stored in the macOS Keychain.")
    else:
        print("Open this URL, authorize the app, then run auth again with the callback URL:")
        print(authorization_url(settings))
        log.emit(correlation_id, "authorization_url_created", "info")
    return 0


def _handle_live(args: argparse.Namespace, settings: Any, correlation_id: str) -> int:
    log = _logger_for(settings)
    log.emit(correlation_id, "command_started", "debug", command=args.command)
    pipeline = Pipeline(settings, correlation_id=correlation_id)
    if args.command == "sync":
        ignore_folders = (args.ignore_folder or []) + settings.ignore_folders
        result = pipeline.sync(args.max_pages, args.dry_run, ignore_folders or None, args.full)
    elif args.command == "probe-bookmarks":
        result = pipeline.probe_bookmarks(args.max_results)
    else:
        result = pipeline.probe_post(args.url)
    log.emit(correlation_id, "command_completed", "info", command=args.command, result=result)
    _print(result)
    return 0


def _handle_gold(args: argparse.Namespace, settings: Any, correlation_id: str) -> int:
    log = _logger_for(settings)
    log.emit(correlation_id, "command_started", "debug", command=args.command)
    database = Database(settings.database_path)
    database.initialize()
    store = GoldStore(database)
    if args.command == "status":
        result = store.status()
        counts = result["counts"]
        log.emit(
            correlation_id,
            "command_completed",
            "info",
            command="status",
            posts=counts["posts"],
            media=counts["media"],
            folders=counts["folders"],
            runs=counts["runs"],
        )
    elif args.command == "search":
        rows = store.search(args.query, args.limit)
        log.emit(
            correlation_id, "command_completed", "info", command="search", matches=len(rows)
        )
        result = rows
    elif args.command == "show":
        result = store.show(args.post_id)
        if result is None:
            raise ValueError(f"post not found: {args.post_id}")
        log.emit(
            correlation_id, "command_completed", "info", command="show", post_id=args.post_id
        )
    elif args.command == "export":
        output = args.output or settings.vault_path / "exports" / f"bookmarks.{args.format}"
        posts = store.export(output, args.format)
        log.emit(correlation_id, "command_completed", "info", command="export", posts=posts)
        result = {"output": str(output), "posts": posts}
    elif args.command == "export-post":
        store.export_post(args.post_id, args.output)
        log.emit(
            correlation_id,
            "command_completed",
            "info",
            command="export-post",
            post_id=args.post_id,
        )
        result = {"output": str(args.output), "post_id": args.post_id}
    elif args.command == "verify":
        result = store.verify(args.full)
        level = "info" if not result["failed"] else "warning"
        log.emit(
            correlation_id,
            "command_completed",
            level,
            command="verify",
            checked=result["checked"],
            failed=result["failed"],
        )
        _print(result)
        return 1 if result["failed"] else 0
    elif args.command == "markdown":
        writer = MarkdownWriter(settings, database, log, correlation_id)
        result = writer.write_new(correlation_id)
        log.emit(correlation_id, "command_completed", "info", command="markdown", **result)
    else:
        result = store.rebuild_silver(settings.vault_path / "bronze")
        log.emit(
            correlation_id,
            "command_completed",
            "info",
            command="rebuild-silver",
            **result,
        )
    _print(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one CLI command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "sync" and args.dry_run and args.max_pages is None:
        parser.error("sync --dry-run requires --max-pages for a bounded API test")
    correlation_id = str(uuid.uuid4())
    settings: Any = None
    try:
        settings = load_settings()
        if args.log_level:
            settings.log_level = args.log_level
        if args.command == "auth":
            return _handle_auth(args, settings, correlation_id)
        if args.command in {"sync", "probe-post", "probe-bookmarks"}:
            return _handle_live(args, settings, correlation_id)
        return _handle_gold(args, settings, correlation_id)
    except (
        AuthError,
        LockAlreadyHeld,
        OSError,
        RequestException,
        sqlite3.Error,
        ValidationError,
        ValueError,
    ) as error:
        if settings is not None:
            _logger_for(settings).emit(
                correlation_id, "command_failed", "error", command=args.command, error=str(error)
            )
        return _print_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
