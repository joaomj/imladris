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

Run a full bookmark sync:

```bash
uv run x-digest sync
```

Limit a test sync to one bookmark page:

```bash
uv run x-digest sync --max-pages 1
```

Preview API reads without writing Bronze or Silver content:

```bash
uv run x-digest sync --max-pages 1 --dry-run
```

The sync stores raw API pages, folder responses, normalized posts, bookmark
membership observations, and media download results.

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
└── scheduler-state.json
```

The project is fully self-contained. Move the entire directory to
relocate everything.

Change the vault location with:

```text
XDIGEST_VAULT_PATH=/path/to/vault
```

Bronze objects are never overwritten or deleted by X Digest.

## Daily Login Sync

Install the per-user macOS LaunchAgent:

```bash
uv run x-digest install-scheduler
```

The agent starts at login and checks once per hour. The durable state guard
allows one successful sync per local calendar day. Network or authentication
failures remain retriable.

## Scope Boundaries

The current version does not include:

- X write operations.
- Summaries or LLM processing.
- X data-export archive import.
- A web interface.
- Multiple X accounts.
- Alternative transports such as browser-cookie clients.
