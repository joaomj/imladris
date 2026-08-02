"""Read-only X API access through the official Python XDK."""

import time
from collections.abc import Iterator
from typing import Any

import requests
from xdk import Client

from .config import Settings

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


class TimeoutSession(requests.Session):
    """Requests session with a configured default timeout."""

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self.timeout = timeout

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return super().request(*args, **kwargs)


def model_dict(model: Any) -> dict[str, Any]:
    """Convert an XDK response model into a JSON-compatible dictionary."""
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json", exclude_none=False)
    if isinstance(model, dict):
        return model
    raise TypeError(f"unsupported XDK response type: {type(model)!r}")


class XApi:
    """Bounded, retrying wrapper around read-only XDK calls."""

    def __init__(self, client: Client, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        session = TimeoutSession(settings.api_timeout_seconds)
        session.headers.update(client.session.headers)
        self.client.session = session

    def _retry(self, operation: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                return operation()
            except requests.RequestException as error:
                last_error = error
                status_code = error.response.status_code if error.response is not None else None
                if status_code is not None and status_code not in {429, 500, 502, 503, 504}:
                    raise
                if attempt >= self.settings.max_retries:
                    raise
                time.sleep(self.settings.retry_base_seconds * (2**attempt))
        raise RuntimeError("retry operation ended without a result") from last_error

    def current_user(self) -> dict[str, Any]:
        """Return the authenticated user record."""
        response = self._retry(lambda: self.client.users.get_me(user_fields=USER_FIELDS))
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
        return model_dict(self._retry(lambda: next(iterator)))

    def folders(self, user_id: str) -> Iterator[dict[str, Any]]:
        """Yield bookmark-folder pages returned by XDK."""
        iterator = self.client.users.get_bookmark_folders(id=user_id)
        for page in iterator:
            yield model_dict(page)

    def folder_posts(self, user_id: str, folder_id: str) -> dict[str, Any]:
        """Fetch posts for one bookmark folder."""
        response = self._retry(
            lambda: self.client.users.get_bookmarks_by_folder_id(user_id, folder_id)
        )
        return model_dict(response)

    def post(self, post_id: str) -> dict[str, Any]:
        """Fetch exactly one post by ID."""
        response = self._retry(
            lambda: self.client.posts.get_by_id(
                id=post_id,
                tweet_fields=POST_FIELDS,
                expansions=EXPANSIONS,
                media_fields=MEDIA_FIELDS,
                user_fields=USER_FIELDS,
            )
        )
        return model_dict(response)

    def posts(self, post_ids: list[str]) -> dict[str, Any]:
        """Fetch details for up to 100 posts in one request."""
        if not post_ids or len(post_ids) > MAX_POST_IDS_PER_REQUEST:
            raise ValueError("batch post retrieval requires between 1 and 100 IDs")
        response = self._retry(
            lambda: self.client.posts.get_by_ids(
                ids=post_ids,
                tweet_fields=POST_FIELDS,
                expansions=EXPANSIONS,
                media_fields=MEDIA_FIELDS,
                user_fields=USER_FIELDS,
            )
        )
        return model_dict(response)
