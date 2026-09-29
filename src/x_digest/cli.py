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
from .brave import BraveReaderError
from .config import load_settings
from .db import Database
from .digest import DigestBuilder, DigestStore
from .digest_service import DigestContext, DigestOptions, DigestService
from .gold import GoldStore
from .lock import LockAlreadyHeld, ProcessLock
from .logging_setup import JsonlLogger
from .markdown import MarkdownWriter
from .pipeline import Pipeline
from .saved_fetch import SavedFetchError
from .saved_pipeline import SavedPipeline


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


def _add_saved_commands(commands: Any) -> None:
    """Register Brave saved-content commands on the subparser collection."""
    brave_sync = commands.add_parser(
        "brave-sync", help="snapshot the Brave reading list and fetch saved content"
    )
    brave_sync.add_argument("--profile", default=None, help="Brave profile name")
    brave_sync.add_argument("--db-path", type=Path, default=None)
    saved_search = commands.add_parser("saved-search", help="search saved content")
    saved_search.add_argument("query")
    saved_search.add_argument("--limit", type=int, default=20)
    saved_show = commands.add_parser("saved-show", help="show one saved URL")
    saved_show.add_argument("url")
    saved_export = commands.add_parser("saved-export", help="export saved items")
    saved_export.add_argument("--format", choices=("markdown", "json"), required=True)
    saved_export.add_argument("--output", type=Path)


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
        "--ignore-account",
        action="append",
        default=[],
        help="skip one X account by username or author ID; repeat to ignore several",
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
    rebuild = commands.add_parser("rebuild-silver", help="rebuild normalized records from Bronze")
    rebuild.add_argument(
        "--ignore-folder",
        action="append",
        default=[],
        help="skip one bookmark folder by name or ID; repeat to ignore several",
    )
    rebuild.add_argument(
        "--ignore-account",
        action="append",
        default=[],
        help="skip one X account by username or author ID; repeat to ignore several",
    )
    commands.add_parser("markdown", help="write Markdown files for posts that do not have one yet")
    digest = commands.add_parser("digest", help="preview or send the weekly digest")
    digest_mode = digest.add_mutually_exclusive_group(required=True)
    digest_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print the selected sources and layout preview without network calls",
    )
    digest_mode.add_argument(
        "--send",
        action="store_true",
        help="generate the digest and send the next pending batch to Telegram",
    )
    digest.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="override digest_max_posts for this delivery",
    )
    digest.add_argument(
        "--model",
        default=None,
        help="override llm_model for this delivery",
    )
    _add_saved_commands(commands)
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


def _handle_digest(args: argparse.Namespace, settings: Any, correlation_id: str) -> int:
    log = _logger_for(settings)
    log.emit(correlation_id, "command_started", "debug", command="digest")
    if args.limit is not None:
        settings.digest_max_posts = args.limit
    if args.model:
        settings.llm_model = args.model
    database = Database(settings.database_path)
    database.initialize()
    store = DigestStore(database, settings)
    builder = DigestBuilder(settings)
    if args.dry_run:
        state = store.load_state()
        limit = args.limit or settings.digest_max_posts
        posts, _has_more, total = store.select_batch(state.get("cursor"), limit)
        _system, _user, prompt_chars, _truncated = builder.build_prompt(posts)
        preview = builder.build_preview(posts, prompt_chars)
        log.emit(
            correlation_id,
            "command_completed",
            "info",
            command="digest",
            mode="dry-run",
            posts=len(posts),
            prompt_chars=prompt_chars,
        )
        _print(
            {
                "mode": "dry-run",
                "text": preview,
                "post_ids": [str(row["post_id"]) for row in posts],
                "prompt_chars": prompt_chars,
                "pending_total": total,
            }
        )
        return 0
    if not settings.telegram_enabled() or not settings.llm_enabled():
        print(
            json.dumps({"error": "digest delivery is not configured"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    context = DigestContext(
        settings=settings,
        database=database,
        log=log,
        correlation_id=correlation_id,
        options=DigestOptions(limit=args.limit, model=args.model),
    )
    with ProcessLock(settings.lock_path):
        counts: dict[str, int] = {}
        result = DigestService(context).deliver(correlation_id, counts)
    log.emit(correlation_id, "command_completed", "info", command="digest", mode="send", **result)
    _print(result)
    return 0 if result.get("status") in {"sent", "partial"} else 1


def _handle_live(args: argparse.Namespace, settings: Any, correlation_id: str) -> int:
    log = _logger_for(settings)
    log.emit(correlation_id, "command_started", "debug", command=args.command)
    pipeline = Pipeline(settings, correlation_id=correlation_id)
    if args.command == "sync":
        ignore_folders = (args.ignore_folder or []) + settings.ignore_folders
        ignore_accounts = (args.ignore_account or []) + settings.ignore_accounts
        result = pipeline.sync(
            max_pages=args.max_pages,
            dry_run=args.dry_run,
            ignore_folders=ignore_folders or None,
            full=args.full,
            ignore_accounts=ignore_accounts or None,
        )
    elif args.command == "probe-bookmarks":
        result = pipeline.probe_bookmarks(args.max_results)
    else:
        result = pipeline.probe_post(args.url)
    log.emit(correlation_id, "command_completed", "info", command=args.command, result=result)
    _print(result)
    return 0


def _handle_saved_sync(args: argparse.Namespace, settings: Any, correlation_id: str) -> int:
    log = _logger_for(settings)
    log.emit(correlation_id, "command_started", "debug", command="brave-sync")
    result = SavedPipeline(settings, correlation_id=correlation_id).sync(
        profile=args.profile, db_path=args.db_path
    )
    log.emit(correlation_id, "command_completed", "info", command="brave-sync", result=result)
    _print(result)
    return 1 if any(result.get(key, 0) for key in ("failed", "exhausted", "fulltext_failed")) else 0


def _handle_saved_search(args: argparse.Namespace, database: Database) -> list[dict[str, Any]]:
    with database.connect() as connection:
        try:
            rows = connection.execute(
                """SELECT s.url, s.title, s.fetch_state, s.final_url,
                          ft.state AS fulltext_state, ft.archive_path AS fulltext_path
                   FROM saved_items_fts f JOIN saved_items s ON s.url = f.url
                   LEFT JOIN saved_fulltext ft ON ft.url=s.url
                   WHERE saved_items_fts MATCH ? LIMIT ?""",
                (args.query, args.limit),
            ).fetchall()
        except sqlite3.OperationalError as error:
            message = str(error).lower()
            if "fts5" not in message and "syntax" not in message:
                raise
            rows = connection.execute(
                """SELECT s.url, s.title, s.fetch_state, s.final_url,
                          ft.state AS fulltext_state, ft.archive_path AS fulltext_path
                   FROM saved_items s LEFT JOIN saved_fulltext ft ON ft.url=s.url
                   WHERE s.title LIKE ? OR s.content_text LIKE ? OR s.url LIKE ? LIMIT ?""",
                (f"%{args.query}%", f"%{args.query}%", f"%{args.query}%", args.limit),
            ).fetchall()
    return [dict(row) for row in rows]


def _saved_record(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """Expose the source record and its separate full-text artifact."""
    record = dict(row)
    fulltext = connection.execute(
        "SELECT * FROM saved_fulltext WHERE url=?", (record["url"],)
    ).fetchone()
    record["fulltext"] = dict(fulltext) if fulltext is not None else None
    if record["fulltext"] is not None:
        record["fulltext"]["identifiers"] = json.loads(record["fulltext"].pop("identifiers_json"))
    return record


def _fulltext_markdown(record: dict[str, Any]) -> str:
    fulltext = record.get("fulltext")
    if fulltext is None:
        return ""
    lines = [f"Full text: {fulltext['state']}"]
    for key in ("content_type", "archive_path", "provider", "license", "error"):
        if fulltext.get(key):
            lines.append(f"{key}: {fulltext[key]}")
    return "\n\n".join(lines) + "\n\n"


def _handle_saved_export(
    args: argparse.Namespace, settings: Any, database: Database
) -> dict[str, Any]:
    output = args.output or settings.vault_path / "exports" / f"saved.{args.format}"
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM saved_items ORDER BY first_seen_at, url"
        ).fetchall()
        records = [_saved_record(connection, row) for row in rows]
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "json":
        output.write_text(json.dumps(records, indent=2, ensure_ascii=False, default=str))
    else:
        blocks = [
            f"# {record.get('title') or record['url']}\n\n"
            f"Source: {record['url']}\n\n"
            f"State: {record.get('fetch_state')}\n\n"
            f"{_fulltext_markdown(record)}{record.get('content_text') or ''}"
            for record in records
        ]
        output.write_text("\n\n---\n\n".join(blocks))
    return {"output": str(output), "items": len(records)}


def _handle_saved(args: argparse.Namespace, settings: Any, correlation_id: str) -> int:
    if args.command == "brave-sync":
        return _handle_saved_sync(args, settings, correlation_id)
    log = _logger_for(settings)
    log.emit(correlation_id, "command_started", "debug", command=args.command)
    database = Database(settings.database_path)
    database.initialize()
    if args.command == "saved-search":
        result: Any = _handle_saved_search(args, database)
        log.emit(
            correlation_id, "command_completed", "info", command="saved-search", matches=len(result)
        )
    elif args.command == "saved-show":
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM saved_items WHERE url = ?", (args.url,)
            ).fetchone()
            if row is None:
                raise ValueError(f"saved URL not found: {args.url}")
            result = _saved_record(connection, row)
        log.emit(correlation_id, "command_completed", "info", command="saved-show", url=args.url)
    else:
        result = _handle_saved_export(args, settings, database)
        log.emit(correlation_id, "command_completed", "info", command="saved-export", **result)
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
        log.emit(correlation_id, "command_completed", "info", command="search", matches=len(rows))
        result = rows
    elif args.command == "show":
        result = store.show(args.post_id)
        if result is None:
            raise ValueError(f"post not found: {args.post_id}")
        log.emit(correlation_id, "command_completed", "info", command="show", post_id=args.post_id)
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
    elif args.command == "rebuild-silver":
        ignore_folders = (args.ignore_folder or []) + settings.ignore_folders
        ignore_accounts = (args.ignore_account or []) + settings.ignore_accounts
        result = store.rebuild_silver(
            settings.vault_path / "bronze",
            ignore_folders or None,
            ignore_accounts or None,
        )
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
        if args.command == "digest":
            return _handle_digest(args, settings, correlation_id)
        if args.command in {"brave-sync", "saved-search", "saved-show", "saved-export"}:
            return _handle_saved(args, settings, correlation_id)
        return _handle_gold(args, settings, correlation_id)
    except (
        AuthError,
        BraveReaderError,
        LockAlreadyHeld,
        OSError,
        RequestException,
        SavedFetchError,
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
