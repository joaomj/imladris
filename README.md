# X Digest

X Digest keeps a private, local copy of your X bookmarks. It reads data from
the official X API, stores the raw responses and media, and builds a searchable
SQLite catalog.

This first version does not create posts, generate summaries, or use an LLM.

## How It Works

```mermaid
sequenceDiagram
    autonumber
    actor Owner
    participant CLI as x-digest CLI
    participant Keychain as macOS Keychain
    participant Pipeline
    participant X as Official X API
    participant Bronze
    participant Silver as Silver SQLite
    participant Media as Media host
    participant Markdown as Markdown vault
    participant Gold as Gold operations
    participant Logs as JSONL logs

    Owner->>CLI: Run auth
    CLI->>Keychain: Store PKCE state
    CLI-->>Owner: Show authorization URL
    Owner->>CLI: Submit callback URL
    CLI->>Keychain: Store OAuth token
    Note over Owner,Keychain: Authorization is completed once. Later runs reuse the token.

    Owner->>CLI: Run sync or probe
    CLI->>Pipeline: Start run
    Pipeline->>Keychain: Load or refresh token
    Keychain-->>Pipeline: Access token
    Pipeline->>X: Read current user and bookmark pages

    loop Incremental bookmark pages
        X-->>Pipeline: Posts and next cursor
        Pipeline->>Bronze: Append raw page
        Pipeline->>Silver: Normalize, index, and save checkpoint
        opt Whole page is already archived
            Pipeline->>Pipeline: Stop reading older pages
        end
    end

    Pipeline->>X: Read bookmark folders
    X-->>Pipeline: Folder names and IDs
    loop Each non-ignored folder
        Pipeline->>X: Read folder post IDs
        X-->>Pipeline: Post IDs
        Pipeline->>Bronze: Append folder response
        Pipeline->>Silver: Record folder membership
        opt Content is missing or sync is full
            Pipeline->>X: Fetch post content, up to 100 IDs per request
            X-->>Pipeline: Full post responses
            Pipeline->>Bronze: Append hydrated responses
            Pipeline->>Silver: Normalize and index content
        end
    end

    Pipeline->>Media: Download pending media
    Media-->>Pipeline: Media bytes or failure
    Pipeline->>Bronze: Store media and download result
    Pipeline->>Markdown: Write missing post files
    Bronze-->>Markdown: Reuse shared media paths
    Pipeline->>Bronze: Write run manifest
    Pipeline->>Logs: Write run status, counts, and events
    Pipeline-->>CLI: Return run result
    CLI-->>Owner: Print result

    Owner->>CLI: Run search, export, verify, or rebuild
    CLI->>Gold: Execute local command
    Gold->>Silver: Query normalized records
    opt Verify archive
        Gold->>Bronze: Check payload and media hashes
    end
    opt Rebuild Silver
        Gold->>Bronze: Replay immutable raw objects
        Bronze-->>Silver: Rebuild normalized records
    end
    Gold-->>CLI: Return results or write exports
    CLI-->>Owner: Print result
```

The pipeline is synchronous and runs only when the owner invokes the CLI. Bronze
is the append-only source layer; Silver and the Markdown files are derived local
outputs. The application performs read-only X operations and keeps the archive
inside the local `data/` vault.

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

Run the normal bookmark sync. It reads new bookmarks (X returns bookmarks
newest-first), then folders, then media:

```bash
uv run x-digest sync
```

The sync is incremental. X Digest stops reading bookmark pages as soon as a
whole page contains posts that are already archived, so an unchanged archive
costs exactly one API call per run. Folder post contents are re-fetched only
for posts that are not archived with complete content yet.

Run a complete re-read to detect edits to old posts and re-hydrate all folder
content:

```bash
uv run x-digest sync --full
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

Skip one or more bookmark folders by name or ID. Their posts are never fetched,
archived, or indexed:

```bash
uv run x-digest sync --ignore-folder spam --ignore-folder 'folder id'
```

Set the same list in `.env` with a comma-separated value:

```text
XDIGEST_IGNORE_FOLDERS=spam
```

## Logs And Measured Cost Data

Every command writes structured JSONL logs. The aggregate log rotates by size,
and every sync or probe run also has its own immutable log file:

```text
<project-root>/data/logs/
├── application.jsonl            # rotated aggregate (5 MB x 5 backups)
├── application.jsonl.1 ...      # rotated backups
└── runs/<run_id>.jsonl          # one immutable file per run
```

Every log line carries a `correlation_id`. For sync and probe runs, the
correlation ID equals the run ID, so one ID links the log files, the `runs`
table, `run_events`, Bronze objects, and observations.

Control the log level with `--log-level` or the environment variable:

```bash
uv run x-digest --log-level debug sync
```

```text
XDIGEST_LOG_LEVEL=info
```

The end of every sync run records measured X API usage: request count per
endpoint, retried attempts, and the lowest `x-rate-limit-remaining` and
`x-app-limit-remaining` values observed. This data appears in the `api_usage`
log event and in the run summary shown by `status`. A warning is logged when a
rate limit falls below 20% of its capacity. The log size settings are
`XDIGEST_LOG_MAX_BYTES` (default 5000000) and `XDIGEST_LOG_BACKUPS` (default 5).

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

Rebuild the normalized database from the immutable raw layer. The ignore list
(`--ignore-folder` or `XDIGEST_IGNORE_FOLDERS`) is applied, so ignored folders
stay out of the rebuilt records:

```bash
uv run x-digest rebuild-silver
```

## Markdown Output

Every sync and probe run writes a Markdown file for each newly archived post,
following your bookmark organization:

```text
data/markdown/
├── posts/                  # posts bookmarked directly, in no folder
│   └── 1234567890.md
└── folders/                # posts organized by bookmark folder
    └── agents/
        └── 9876543210.md
```

A post that belongs to several folders gets one file in each folder directory.
Unsafe characters in folder names are replaced with `_`; two folders with the
same name get their ID appended to the directory name.

Each file contains the post text (or full Article body), its author, date, and
source URL. When a post has downloaded media, the file references it: images
appear inline with `![media](path)` and videos and other media appear as
clickable links. The paths point at the media files inside `data/bronze/`, so
there is only one copy of each media file.

Markdown files follow the same policy as the database records: a file is
written once when the post is first archived and is never regenerated, so
hand-made edits are safe. Posts that have neither text nor media produce no
file.

Generate Markdown for all posts that still lack a file, without any sync and
without consuming X API quota:

```bash
uv run x-digest markdown
```

The command is local and idempotent: it only creates missing files.

## Local Storage

The vault lives in `data/` inside the project root:

```text
<project-root>/data/
├── bronze/              # immutable API responses + media
├── silver.sqlite        # normalized records + FTS5 index
├── markdown/posts/      # one Markdown file per archived post
├── logs/                # rotated aggregate + per-run log files
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

These items are intentionally deferred.

- Confirm from current official X API documentation whether the bookmarks
  endpoint supports incremental reads, such as `since_id` or an equivalent
  cursor strategy. The current incremental sync stops at already-archived
  pages instead, which costs one API call per unchanged run.
- Change folder synchronization to run weekly because folders rarely change.
- Record the current X API pricing, with the source date and source URL.
- Estimate daily and weekly costs for bookmark pages, folder reads, retries,
  token refreshes, and media downloads, using the measured usage in the logs.

## Scope Boundaries

The current version does not include:

- X write operations.
- Summaries or LLM processing.
- X data-export archive import.
- A web interface.
- Multiple X accounts.
- Alternative transports such as browser-cookie clients.
