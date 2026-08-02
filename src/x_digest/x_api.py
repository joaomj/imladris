"""Read-only X API access through the official Python XDK."""

import time
import urllib.parse
import uuid
from collections.abc import Iterator
from typing import Any

import requests
from xdk import Client

from .config import Settings
from .logging_setup import JsonlLogger

POST_FIELDS = [
    "article",
    "attachments",
    "author_id",
    "created_at",
    "entities",
    "lang",
    "note_tweet",
    "public_metrics",
    "referenced_tweets",
    "text",
]
EXPANSIONS = [
    "article.cover_media",
    "article.media_entities",
    "attachments.media_keys",
    "author_id",
    "referenced_tweets.id",
]
MEDIA_FIELDS = [
    "alt_text",
    "duration_ms",
    "height",
    "media_key",
    "type",
    "url",
    "variants",
    "width",
]
USER_FIELDS = ["created_at", "description", "id", "name", "public_metrics", "url", "username"]
MAX_POST_IDS_PER_REQUEST = 100
LOW_CAPACITY_RATIO = 0.2


class TimeoutSession(requests.Session):
    """Requests session with a configured default timeout."""

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self.timeout = timeout

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return super().request(*args, **kwargs)


class UsageTracker:
    """Measure real X API usage from response headers."""

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}
        self._retries: dict[str, int] = {}

    def record(self, path: str, response: requests.Response) -> None:
        """Record one HTTP attempt and its rate-limit headers."""
        entry = self._entries.setdefault(
            path,
            {
                "requests": 0,
                "min_rate_limit_remaining": None,
                "min_app_limit_remaining": None,
            },
        )
        entry["requests"] += 1
        for header, target in (
            ("x-rate-limit-remaining", "min_rate_limit_remaining"),
            ("x-app-limit-remaining", "min_app_limit_remaining"),
        ):
            value = response.headers.get(header)
            if value is not None:
                numeric = int(value)
                current = entry[target]
                entry[target] = numeric if current is None else min(current, numeric)

    def record_retry(self, endpoint: str) -> None:
        """Record one retried attempt for an endpoint label."""
        self._retries[endpoint] = self._retries.get(endpoint, 0) + 1

    def summary(self) -> dict[str, dict[str, Any]]:
        """Return measured usage grouped by endpoint path."""
        return {path: dict(entry) for path, entry in self._entries.items()}

    def retries(self) -> dict[str, int]:
        """Return retried attempt counts grouped by endpoint label."""
        return dict(self._retries)


class UsageTrackingSession(TimeoutSession):
    """Requests session that records usage and warns on low rate-limit capacity."""

    def __init__(
        self,
        timeout: float,
        tracker: UsageTracker,
        log: JsonlLogger | None,
        correlation_id: str | None,
    ) -> None:
        super().__init__(timeout)
        self.tracker = tracker
        self.log = log
        self.correlation_id = correlation_id

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        path = urllib.parse.urlparse(url).path
        try:
            response = super().request(method, url, **kwargs)
        except requests.RequestException:
            self.tracker.record_retry(path)
            raise
        self.tracker.record(path, response)
        remaining = response.headers.get("x-rate-limit-remaining")
        limit = response.headers.get("x-rate-limit-limit")
        if (
            self.log
            and remaining is not None
            and limit is not None
            and int(limit) > 0
            and int(remaining) / int(limit) < LOW_CAPACITY_RATIO
        ):
            self.log.emit(
                self.correlation_id or "unknown",
                "low_rate_limit",
                "warning",
                endpoint=path,
                remaining=int(remaining),
                limit=int(limit),
            )
        return response


def model_dict(model: Any) -> dict[str, Any]:
    """Convert an XDK response model into a JSON-compatible dictionary."""
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json", exclude_none=False)
    if isinstance(model, dict):
        return model
    raise TypeError(f"unsupported XDK response type: {type(model)!r}")


class XApi:
    """Bounded, retrying wrapper around read-only XDK calls."""

    def __init__(
        self,
        client: Client,
        settings: Settings,
        log: JsonlLogger | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.log = log
        self.correlation_id = correlation_id
        self.tracker = UsageTracker()
        session = UsageTrackingSession(
            settings.api_timeout_seconds, self.tracker, log, correlation_id
        )
        session.headers.update(client.session.headers)
        self.client.session = session

    def _retry(self, endpoint: str, operation: Any) -> Any:
        request_id = uuid.uuid4().hex[:12]
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            if self.log:
                self.log.emit(
                    self.correlation_id or "unknown",
                    "api_attempt",
                    "debug",
                    request_id=request_id,
                    endpoint=endpoint,
                    attempt=attempt,
                )
            try:
                result = operation()
                if self.log:
                    self.log.emit(
                        self.correlation_id or "unknown",
                        "api_request_ok",
                        "debug",
                        request_id=request_id,
                        endpoint=endpoint,
                        attempt=attempt,
                    )
                return result
            except requests.RequestException as error:
                last_error = error
                status_code = error.response.status_code if error.response is not None else None
                if status_code is not None and status_code not in {429, 500, 502, 503, 504}:
                    if self.log:
                        self.log.emit(
                            self.correlation_id or "unknown",
                            "api_request_failed",
                            "warning",
                            request_id=request_id,
                            endpoint=endpoint,
                            attempt=attempt,
                            status=status_code,
                            error=str(error),
                        )
                    raise
                if attempt >= self.settings.max_retries:
                    if self.log:
                        self.log.emit(
                            self.correlation_id or "unknown",
                            "api_request_failed",
                            "warning",
                            request_id=request_id,
                            endpoint=endpoint,
                            attempt=attempt,
                            status=status_code,
                            error=str(error),
                        )
                    raise
                self.tracker.record_retry(endpoint)
                if self.log:
                    self.log.emit(
                        self.correlation_id or "unknown",
                        "api_retry",
                        "debug",
                        request_id=request_id,
                        endpoint=endpoint,
                        attempt=attempt,
                        status=status_code,
                    )
                time.sleep(self.settings.retry_base_seconds * (2**attempt))
        raise RuntimeError("retry operation ended without a result") from last_error

    def current_user(self) -> dict[str, Any]:
        """Return the authenticated user record."""
        response = self._retry(
            "users/me", lambda: self.client.users.get_me(user_fields=USER_FIELDS)
        )
        return model_dict(response)

    def bookmark_page(
        self, user_id: str, cursor: str | None, max_results: int | None = None
    ) -> dict[str, Any]:
        """Fetch exactly one bookmark page."""
        iterator = self.client.users.get_bookmarks(
            id=user_id,
            max_results=max_results or self.settings.max_results_per_page,
            pagination_token=cursor,
            tweet_fields=POST_FIELDS,
            expansions=EXPANSIONS,
            media_fields=MEDIA_FIELDS,
            user_fields=USER_FIELDS,
        )
        return model_dict(self._retry("users/bookmarks", lambda: next(iterator)))

    def folders(self, user_id: str) -> Iterator[dict[str, Any]]:
        """Yield bookmark-folder pages returned by XDK."""
        iterator = self.client.users.get_bookmark_folders(id=user_id)
        for page in iterator:
            yield model_dict(page)

    def folder_posts(self, user_id: str, folder_id: str) -> dict[str, Any]:
        """Fetch posts for one bookmark folder."""
        response = self._retry(
            "users/bookmarks/folders",
            lambda: self.client.users.get_bookmarks_by_folder_id(user_id, folder_id),
        )
        return model_dict(response)

    def post(self, post_id: str) -> dict[str, Any]:
        """Fetch exactly one post by ID."""
        response = self._retry(
            "posts/id",
            lambda: self.client.posts.get_by_id(
                id=post_id,
                tweet_fields=POST_FIELDS,
                expansions=EXPANSIONS,
                media_fields=MEDIA_FIELDS,
                user_fields=USER_FIELDS,
            ),
        )
        return model_dict(response)

    def posts(self, post_ids: list[str]) -> dict[str, Any]:
        """Fetch details for up to 100 posts in one request."""
        if not post_ids or len(post_ids) > MAX_POST_IDS_PER_REQUEST:
            raise ValueError("batch post retrieval requires between 1 and 100 IDs")
        response = self._retry(
            "posts",
            lambda: self.client.posts.get_by_ids(
                ids=post_ids,
                tweet_fields=POST_FIELDS,
                expansions=EXPANSIONS,
                media_fields=MEDIA_FIELDS,
                user_fields=USER_FIELDS,
            ),
        )
        return model_dict(response)
