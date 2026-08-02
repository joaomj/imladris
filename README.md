# X Digest

X Digest keeps a private, local copy of your X bookmarks. It reads data from
the official X API, stores the raw responses and media, and builds a searchable
SQLite catalog.

This first version does not create posts, generate summaries, or use an LLM.

## Requirements

- macOS
- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- An X Developer application with OAuth 2.0 PKCE enabled

## Install

```bash
git clone https://github.com/joaomj/x-digest.git
cd x-digest
uv sync
```

Register this redirect URI in the X Developer application:

```text
http://localhost:8080/callback
```

Set the client ID in `.env`:

```text
XDIGEST_X_CLIENT_ID=your-client-id
```

Use the OAuth 2.0 settings shown in the X Developer Console. Set the app
permission to read and enable bookmark, post, and user read scopes. Keep the
client secret in the local `.env` file. Do not commit it or send it in chat.

Set `XDIGEST_X_CLIENT_SECRET` only when the X application requires a client
secret. The `.env` file stays local and is ignored by Git.

## Authorize X

Run:

```bash
uv run x-digest auth
```

Open the printed URL and authorize the application. Copy the complete callback
URL from the browser address bar and run:

```bash
uv run x-digest auth --callback-url 'http://localhost:8080/callback?code=...&state=...'
```

The OAuth token is stored in the macOS Keychain. X Digest does not write an X
post or modify bookmark state.

## Sync Bookmarks

Run the normal bookmark sync. It resumes from the saved checkpoint and reads
the remaining bookmark pages, folders, and media:

```bash
uv run x-digest sync
```

For bounded CLI testing only, limit the read to one bookmark page:

```bash
uv run x-digest sync --max-pages 1
```

For a bounded API test without writing Bronze or Silver content:

```bash
uv run x-digest sync --max-pages 1 --dry-run
```

The sync stores raw API pages, folder responses, normalized posts, bookmark
membership observations, and media download results.
Folder responses contain post IDs only, so X Digest retrieves their contents in
batches of up to 100 IDs before indexing them.

## Inspect Bookmark Samples

After authorization, fetch one bounded bookmark page and exactly one ordinary
post plus one Article from that page:

```bash
uv run x-digest probe-bookmarks --max-results 20
```

The command never paginates through the full bookmark collection. Increase
`--max-results` only when the first bounded page does not contain an Article.

## Inspect One Post

Use `probe-post` to fetch exactly one post. This command is useful for checking
the API response shape for one ordinary post or one X Article:

```bash
uv run x-digest probe-post 'https://x.com/user/status/1234567890'
```

It does not enumerate bookmarks.

## Search And Export

```bash
uv run x-digest status
uv run x-digest search "local archive"
uv run x-digest show 1234567890
uv run x-digest export-post 1234567890 --output ./post.md
uv run x-digest export --format markdown
uv run x-digest export --format json --output ./bookmarks.json
```

Verify the archive:

```bash
uv run x-digest verify --full
```

Rebuild the normalized database from the immutable raw layer:

```bash
uv run x-digest rebuild-silver
```

## Local Storage

The vault lives in `data/` inside the project root:

```text
<project-root>/data/
├── bronze/              # immutable API responses + media
├── silver.sqlite        # normalized records + FTS5 index
├── logs/application.jsonl
```

The project is fully self-contained. Move the entire directory to
relocate everything.

Change the vault location with:

```text
XDIGEST_VAULT_PATH=/path/to/vault
```

Bronze objects are never overwritten or deleted by X Digest.

X Digest runs manually through the CLI. It does not install or run a background
scheduler.

## Next Steps

These items are intentionally deferred. The current sync behavior remains
unchanged until the investigation is complete.

- Confirm from current official X API documentation whether the bookmarks
  endpoint supports incremental reads, such as `since_id` or an equivalent
  cursor strategy.
- Design a low-cost daily bookmark sync that avoids rereading the complete
  archive while preserving recovery and archive integrity.
- Change folder synchronization to run weekly because folders rarely change.
- Record the current X API pricing, with the source date and source URL.
- Estimate daily and weekly costs for bookmark pages, folder reads, retries,
  token refreshes, and media downloads.

## Scope Boundaries

The current version does not include:

- X write operations.
- Summaries or LLM processing.
- X data-export archive import.
- A web interface.
- Multiple X accounts.
- Alternative transports such as browser-cookie clients.
