"""Shared digest delivery used by automatic sync and manual sends."""

from dataclasses import dataclass
from typing import Any

from .config import Settings
from .db import Database
from .digest import DigestBuilder, DigestStore
from .llm import LlmClient
from .logging_setup import JsonlLogger
from .telegram import TelegramSender

DIGEST_ERROR_PREVIEW_CHARS = 500


def _safe_error(error: Exception, sender: TelegramSender | None = None) -> str:
    """Return a sanitized error summary without secrets or response bodies."""
    if sender is not None:
        details = sender.safe_details(error)
        return str(details.get("category") or type(error).__name__)
    return type(error).__name__


@dataclass
class ReadyBatch:
    """Frozen batch ready for Telegram delivery."""

    posts: list[dict[str, Any]]
    chunks: list[str]
    next_chunk: int
    state: dict[str, Any]
    prompt_chars: int = 0


@dataclass
class DigestOptions:
    """Optional overrides for one digest delivery."""

    limit: int | None = None
    model: str | None = None


@dataclass
class DigestDependencies:
    """Injectable collaborators for delivery tests."""

    emit: Any = None
    llm: LlmClient | None = None
    sender: TelegramSender | None = None


@dataclass
class DigestContext:
    """Objects required for one digest delivery."""

    settings: Settings
    database: Database
    log: JsonlLogger | None = None
    correlation_id: str | None = None
    options: DigestOptions | None = None
    dependencies: DigestDependencies | None = None


class DigestService:
    """Deliver one pending digest batch with best-effort failure handling."""

    def __init__(self, context: DigestContext) -> None:
        self.settings = context.settings
        self.database = context.database
        self.log = context.log
        self.correlation_id = context.correlation_id or "unknown"
        self.options = context.options or DigestOptions()
        dependencies = context.dependencies or DigestDependencies()
        self.emit = dependencies.emit
        self.store = DigestStore(context.database, context.settings)
        self.builder = DigestBuilder(context.settings)
        self.llm = dependencies.llm or LlmClient(
            context.settings, context.log, self.correlation_id
        )
        self.sender = dependencies.sender or TelegramSender(
            context.settings, context.log, self.correlation_id
        )
        if self.options.model:
            self.settings.llm_model = self.options.model

    def _event(self, event: str, level: str = "info", **details: Any) -> None:
        if self.emit is not None:
            self.emit(event, level, **details)
        elif self.log is not None:
            self.log.emit(self.correlation_id, event, level, stage="digest", **details)

    def deliver(self, run_id: str, counts: dict[str, int]) -> dict[str, Any]:
        """Deliver the next pending batch and update counts without raising."""
        del run_id
        if not self.settings.telegram_enabled() or not self.settings.llm_enabled():
            self._event("digest_skipped", reason="missing_config")
            counts["digest_skipped"] = 1
            return {"status": "skipped", "reason": "missing_config"}
        state = self.store.load_state()
        limit = self.options.limit or self.settings.digest_max_posts
        try:
            return self._deliver_batch(counts, state, limit)
        except Exception as error:
            preview = _safe_error(error, self.sender)
            self._event("digest_failed", "warning", error=preview[:DIGEST_ERROR_PREVIEW_CHARS])
            counts["digest_failed"] = 1
            return {"status": "failed", "error": preview[:DIGEST_ERROR_PREVIEW_CHARS]}

    def _deliver_batch(
        self,
        counts: dict[str, int],
        state: dict[str, Any],
        limit: int,
    ) -> dict[str, Any]:
        """Select pending posts, summarize, send, and advance the cursor."""
        pending_ids = state.get("pending_ids")
        chunks = state.get("chunks")
        next_chunk = int(state.get("next_chunk") or 0)
        if isinstance(pending_ids, list) and isinstance(chunks, list) and chunks:
            posts = self.store.get_posts_by_ids([str(item) for item in pending_ids])
            ready = ReadyBatch(posts, chunks, next_chunk, state)
            return self._send_ready_batch(ready, counts, limit)
        posts, _has_more, total = self.store.select_batch(state.get("cursor"), limit)
        if not posts:
            return self._send_heartbeat(counts, total)
        system, user, prompt_chars, _truncated = self.builder.build_prompt(posts)
        raw = self.llm.complete(user, system)
        parsed = DigestBuilder.parse_response(raw, {str(row["post_id"]) for row in posts})
        rendered = DigestBuilder.render_html(
            parsed, {str(row["post_id"]): row for row in posts}
        )
        ready_chunks = TelegramSender.split_chunks(rendered)
        ready_state = {
            "cursor": state.get("cursor"),
            "pending_ids": [str(row["post_id"]) for row in posts],
            "chunks": ready_chunks,
            "next_chunk": 0,
        }
        self.store.save_state(ready_state)
        ready = ReadyBatch(posts, ready_chunks, 0, ready_state, prompt_chars)
        return self._send_ready_batch(ready, counts, limit)

    def _send_heartbeat(self, counts: dict[str, int], total: int) -> dict[str, Any]:
        """Send the empty-queue message without calling the LLM."""
        rendered = DigestBuilder.render_heartbeat(total == 0)
        ready = TelegramSender.split_chunks(rendered)
        sent = self.sender.send_chunks(ready, 0)
        self._record_sent(counts, ReadyBatch([], ready, 0, {}), sent, 0)
        self._event(
            "digest_sent",
            chunks=len(ready),
            posts=0,
            chars=sum(len(chunk) for chunk in ready),
            backlog=0,
            heartbeat=True,
        )
        return {"status": "sent", "heartbeat": True, "chunks": len(ready)}

    def _send_ready_batch(
        self,
        ready: ReadyBatch,
        counts: dict[str, int],
        limit: int,
    ) -> dict[str, Any]:
        """Send frozen chunks and advance the cursor only after full success."""
        sent = self.sender.send_chunks(ready.chunks, ready.next_chunk)
        if ready.next_chunk + sent < len(ready.chunks):
            return self._record_partial(ready, sent, counts)
        cursor = DigestStore.cursor_for(ready.posts)
        if cursor is not None:
            self.store.clear_batch(cursor)
        backlog = self.store.pending_count(cursor) if cursor else 0
        self._record_sent(counts, ready, len(ready.chunks), backlog)
        if ready.prompt_chars:
            counts["digest_prompt_chars"] = ready.prompt_chars
        self._event(
            "digest_sent",
            chunks=len(ready.chunks),
            posts=len(ready.posts),
            chars=sum(len(chunk) for chunk in ready.chunks),
            backlog=backlog,
            has_more=backlog > 0,
            post_ids=[str(row["post_id"]) for row in ready.posts[:limit]],
        )
        return {
            "status": "sent",
            "chunks": len(ready.chunks),
            "posts": len(ready.posts),
            "backlog": backlog,
            "has_more": backlog > 0,
        }

    def _record_partial(
        self,
        ready: ReadyBatch,
        sent: int,
        counts: dict[str, int],
    ) -> dict[str, Any]:
        """Persist partial progress so the next run resumes mid-batch."""
        ready.state["next_chunk"] = sent
        self.store.save_state(ready.state)
        self._event(
            "digest_partial",
            "warning",
            chunks=len(ready.chunks),
            sent=sent,
            posts=len(ready.posts),
        )
        counts["digest_chunks"] = len(ready.chunks)
        counts["digest_posts"] = len(ready.posts)
        return {"status": "partial", "chunks": len(ready.chunks), "sent": sent}

    def _record_sent(
        self,
        counts: dict[str, int],
        summary: ReadyBatch,
        sent: int,
        remaining: int,
    ) -> None:
        counts["digest_chunks"] = len(summary.chunks)
        counts["digest_sent_chunks"] = sent
        counts["digest_posts"] = len(summary.posts)
        counts["digest_chars"] = sum(len(chunk) for chunk in summary.chunks)
        counts["digest_backlog"] = remaining
