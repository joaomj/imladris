# X Digest

X Digest archives X bookmarks and the Brave Reading List in local files and a
searchable SQLite catalog. Collection does not modify X or Brave and does not
use an LLM. An optional Telegram digest uses OpenRouter to summarize X posts.

## Key Capabilities

- Private archive in the local `data/` directory.
- Incremental X bookmark sync and read-only Brave Reading List collection.
- PubMed metadata and licensed PMC full-text XML.
- Bookmark folder archive with an ignore list.
- One Markdown file per archived post.
- Search, export, verify, and rebuild commands.

## Requirements

- macOS
- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- For X: an X Developer application with OAuth 2.0 PKCE enabled.
- For Brave: a local Brave profile, Donsetch, and the optional `brave` extra.

For Brave-only use, skip X configuration and authorization. See
[Archive the Brave Reading List](#archive-the-brave-reading-list).

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

Copy the template and set the client credentials:

```bash
cp .env.example .env
```

```text
XDIGEST_X_CLIENT_ID=your-client-id
XDIGEST_X_CLIENT_SECRET=your-client-secret
```

Set `XDIGEST_X_CLIENT_SECRET` only when the X application requires one. The
`.env` file stays local and is ignored by Git.

Enable the bookmark, post, and user read permissions in the X Developer
Console.

## Authorize X

```bash
uv run x-digest auth
```

Open the printed URL and authorize the application. Copy the complete callback
URL from the browser address bar and run:

```bash
uv run x-digest auth --callback-url 'http://localhost:8080/callback?code=...&state=...'
```

The OAuth token is stored in the macOS Keychain. Authorization runs once;
later commands reuse the token.

## Core Usage

### Sync bookmarks

```bash
uv run x-digest sync
```

The sync is incremental: it stops as soon as a page contains only
already-archived posts. See `tech-context.md`, section 13.5 for the details.

Force a complete re-read:

```bash
uv run x-digest sync --full
```

Skip folders by name or ID. Their posts are never fetched, archived, or
indexed:

```bash
uv run x-digest sync --ignore-folder spam
```

Set the same list in `.env`:

```text
XDIGEST_IGNORE_FOLDERS=spam
```

Skip X accounts by username or author ID. Matching posts are never archived,
indexed, or digested:

```bash
uv run x-digest sync --ignore-account spammy
```

Set the same list in `.env` (local only, never committed):

```text
XDIGEST_IGNORE_ACCOUNTS=spammy,@other,1234567890
```

Author IDs are the stable choice: they survive handle renames. Ignoring a
folder goes further — each sync learns every author currently listed in
that folder and blocks them too, persisting the IDs in the local vault
(`ignore-accounts:auto` checkpoint, never committed). Posts from blocked
authors are purged before Markdown and digest delivery.

### Browse and export

```bash
uv run x-digest status
uv run x-digest search "local archive"
uv run x-digest show 1234567890
uv run x-digest export --format markdown
uv run x-digest export-post 1234567890 --output ./post.md
```

### Verify and rebuild

```bash
uv run x-digest verify --full
uv run x-digest rebuild-silver
```

`verify` checks the archive; `rebuild-silver` rebuilds the searchable database
from the raw archive and applies the ignore list.

### Inspect API samples

```bash
uv run x-digest probe-bookmarks --max-results 20
uv run x-digest probe-post 'https://x.com/user/status/1234567890'
```

The probe commands fetch bounded samples and never paginate the full bookmark
collection.

### Generate missing Markdown

Every sync writes one Markdown file per newly archived post. A file is written
once and never regenerated, so hand-made edits are safe. Generate files for all
posts that still lack one, without any sync:

```bash
uv run x-digest markdown
```

### Archive the Brave Reading List

Brave ingestion is separate from X bookmark sync and does not require X authorization.
Install [Donsetch](https://github.com/dondai44423/donsetch) for webpage extraction.
The command checks `PATH`, then `~/.local/bin/donsetch` for a user-local installation.
Set `XDIGEST_DONSETCH_BIN` to select a different executable. A missing executable
stops the run before reading Brave or consuming any item retries.
The optional `brave` extra contains one pure-Python LevelDB reader; no system
LevelDB or Snappy installation is required.

```bash
uv run --extra brave x-digest brave-sync --profile Default
```

Use `--db-path` to override the profile's `Sync Data/LevelDB` directory.
Browse the local saved-content catalog without contacting websites:

```bash
uv run x-digest saved-search "education"
uv run x-digest saved-show 'https://example.org/article'
uv run x-digest saved-export --format json
```

`verify --full` includes archived PDFs and PMC XML. `rebuild-silver` also replays saved-content
Bronze records. PDFs are searchable by saved title and URL, not by PDF text.

The reader copies only the profile's LevelDB files into a temporary directory.
It never opens the original files as a database or changes Brave settings or entries.
Brave can remain open. The reader retries if the files change during copying;
if it cannot obtain a stable copy, it reports an error.
On macOS, the process that runs the command needs permission to read Brave's data.

The workflow saves the URL-list snapshot in Bronze before fetching content:

- Webpages: archive Donsetch's extraction response, including Markdown and metadata.
- PubMed article URLs: fetch the exact PMID through the official NCBI EFetch API.
  Archive the raw XML bytes as base64 and render the metadata and available abstract.
  Bronze labels these results as `saved-api-record`, not webpage extractions or full papers.
- PDFs: archive the original file bytes. Do not convert PDFs to Markdown.
- Inaccessible pages: retain the saved URL and record the failure.

Ordinary webpages use only the saved URL and normal HTTP redirects. Scholarly
URLs have an approved exception: PubMed IDs, PMC IDs, and DOI URLs can identify
the same paper through official APIs. The command does not infer papers from
arbitrary page text, follow references, or crawl sites.

Full-text retrieval uses PMC EFetch when a PMC ID is known from the saved URL
or its PubMed record. DOI-only saves do not trigger a DOI-to-PMC search.
No email, API key, or hosted discovery service is required. Licensed article XML
is archived unchanged and separately from the saved record and its abstract.
Linked figures are not downloaded. Explicitly saved PDFs still use the existing
downloader and remain unchanged.

Provider, source URL, license, format, hash, and diagnostics are retained in
`saved-fulltext` Bronze records and the `saved_fulltext` table. Use `saved-show`
or `saved-export` to inspect the full-text state and file path. If PMC full text
cannot be obtained, the saved record remains available and the limitation is explicit.
An `unavailable` result does not claim that no full text exists elsewhere and
does not fail the sync. Network and malformed-response failures remain distinct
and use at most `XDIGEST_SAVED_MAX_ATTEMPTS` total attempts; unresolved failures
fail the sync.
Downloaded and confirmed unavailable outcomes are not requested again on later runs.
Set `XDIGEST_SAVED_FULLTEXT_ENABLED=false` to disable this additional retrieval.
Successful PMC XML retrieval does not clear a failed original PDF download.
Both outcomes remain visible, and the PDF failure still causes a nonzero exit.
No hosted extraction fallbacks or LLM routing are used.

X/Twitter and Reddit URLs remain in the snapshot but are excluded from webpage
collection. X content belongs to the X bookmarks source. Existing archived
content is preserved.
Completed URLs are skipped on later runs. Metadata changes do not trigger another
content fetch. Removing an item from Brave does not remove its archive.
Failed URLs retry on subsequent runs, up to `XDIGEST_SAVED_MAX_ATTEMPTS` total
attempts. Exhausted failures remain visible and cause a nonzero command exit.
Truncated or empty extraction results are failures, not completed items.

PDF text extraction and Brave digest delivery are not enabled by this command.
The command does not install a schedule. To automate both sources, install the
combined collection job described below.

## Configuration

The complete settings list is in `tech-context.md`, section 7. Common `.env`
settings:

| Setting | Purpose | Default |
| --- | --- | --- |
| `XDIGEST_X_CLIENT_ID` | X Developer application client ID | None |
| `XDIGEST_X_CLIENT_SECRET` | Optional X client secret | None |
| `XDIGEST_X_REDIRECT_URI` | OAuth callback URI | `http://localhost:8080/callback` |
| `XDIGEST_IGNORE_FOLDERS` | Comma-separated folder names or IDs to skip | empty |
| `XDIGEST_IGNORE_ACCOUNTS` | Comma-separated X usernames or author IDs to skip | empty |
| `XDIGEST_FOLDER_SYNC_DAYS` | Minimum days between folder reads | `7` |
| `XDIGEST_VAULT_PATH` | Vault location | `<project-root>/data` |
| `XDIGEST_LOG_LEVEL` | Log level | `info` |
| `XDIGEST_SAVED_FULLTEXT_ENABLED` | Discover open-access full text for known scholarly identifiers | `true` |

## Local Storage

```text
<project-root>/data/
├── bronze/              # snapshots, responses, PDFs, XML, and media
├── silver.sqlite        # normalized records and search index
├── markdown/            # one Markdown file per archived post
└── logs/                # aggregate and per-run logs
```

The project is self-contained. Move the entire `data/` directory to relocate
everything.

## Automated Weekly Sync

Install the launchd agents for combined collection every Sunday at 06:00 and
GCS backup at 06:15. The collection job runs X sync, then Brave sync. Both
stages run even if one fails; the job reports failure if either stage fails.
The backup waits for the combined job to finish, up to 30 minutes.

Re-run the installer to replace an existing X-only job. It retains the same
launchd label and schedule and does not start collection immediately.
Brave uses `XDIGEST_BRAVE_PROFILE` or `Default`; Donsetch must be installed
for the same macOS user. The launchd process must have permission to read
Brave's profile. A successful Terminal run alone does not verify this access.

```bash
./scripts/install-scheduler.sh
./scripts/install-backup-scheduler.sh
```

The optional shell trigger provides another way to start collection: the first interactive shell each ISO week starts
the same two agents in the background after a 30-minute delay, once per
week. To enable it, source `scripts/zshrc-init.sh` from `~/.zshrc`. The scheduler
installer does not add this hook.
Progress lands in `data/logs/weekly-trigger.log` with a once-per-week
stamp at `data/logs/weekly-shell-trigger.stamp`.

Remove the agent:

```bash
./scripts/install-scheduler.sh --remove
```

Trigger the first run immediately:

```bash
launchctl kickstart "gui/$(id -u)/com.x-digest.sync"
```

See `tech-context.md`, section 17 for the agent behavior.

## Weekly Telegram digest

The Sunday sync also sends one private Telegram digest after Markdown files
are written. OpenRouter summarizes the oldest undelivered posts as themed key
points with source links. Delivery is best-effort: archive success never
depends on the digest, and failed batches stay pending for the next run.

Configure the bot and OpenRouter key in `.env`:

```text
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_USER_ID=your-chat-id
XDIGEST_LLM_API_KEY=your-openrouter-key
```

Prefer the macOS Keychain over `.env` for the OpenRouter key: store it
under service `x-digest`, account `openrouter-api-key`. A set
`XDIGEST_LLM_API_KEY` value takes precedence over the Keychain entry.

`XDIGEST_TELEGRAM_BOT_TOKEN` and `XDIGEST_TELEGRAM_CHAT_ID` work as prefixed
alternatives. Create the bot with `@BotFather`, send it `/start`, then read
your chat ID from `getUpdates`:

```bash
curl "https://api.telegram.org/botYOUR_TOKEN/getUpdates"
```

Preview the next batch without network calls:

```bash
uv run x-digest digest --dry-run
```

The preview lists selected sources and layout only. Send the next pending
batch manually:

```bash
uv run x-digest digest --send
```

Large weeks stay pending across runs. Remove digest credentials to disable
notifications without changing the archive schedule.

## Backup to Google Cloud Storage (Free Tier)

A weekly backup copies the `data/` directory to a private GCS bucket with
`rclone`. Files are never deleted on the bucket; the backup only grows. Use a
`STANDARD` bucket in an Always Free region (`us-central1`, `us-west1`,
`us-east1`) with uniform bucket-level access, public access prevention
enforced, and 7 day soft delete.

Requirements:

- `rclone` installed (for example via Homebrew).
- A GCS bucket and a `rclone` remote of type `google cloud storage`. Grant the
  backup service account `roles/storage.objectUser` on only the backup bucket.
  Configure the bucket name and remote through `.env`:

  ```text
  XDIGEST_BACKUP_BUCKET=your-gcs-bucket-name
  XDIGEST_BACKUP_REMOTE=gcs
  ```

  The backup reads credentials from the macOS login Keychain. Store the service
  account JSON under service `x-digest` and account `gcs-backup-credentials`:

  ```bash
  chmod 600 "$HOME/.config/gcloud/your-key.json"
  security unlock-keychain "$HOME/Library/Keychains/login.keychain-db"
  security add-generic-password \
    -U \
    -s x-digest \
    -a gcs-backup-credentials \
    -w "$(tr -d '\n' < "$HOME/.config/gcloud/your-key.json")" \
    "$HOME/Library/Keychains/login.keychain-db"
  ```

  An SSH session can use the read-only System keychain by default. The explicit
  login-keychain path prevents `-61 Write permissions error` during setup.

  Verify the stored JSON without printing it:

  ```bash
  security find-generic-password \
    -s x-digest \
    -a gcs-backup-credentials \
    -w \
    "$HOME/Library/Keychains/login.keychain-db" \
    | uv run python -c "import json,sys; d=json.load(sys.stdin); print('Keychain OK:', d.get('type') == 'service_account')"
  ```

  The expected result is `Keychain OK: True`. Run the backup before you remove
  the source JSON file. After a successful backup, remove the file:

  ```bash
  ./scripts/backup-to-drive.sh
  tail -n 8 data/logs/backup.log
  rm "$HOME/.config/gcloud/your-key.json"
  ```

  The log must end with `backup end`. The script exports
  `RCLONE_GCS_SERVICE_ACCOUNT_CREDENTIALS` from Keychain for `rclone`.

  As a file-based fallback, configure the `rclone` remote with the JSON file:

  ```bash
  rclone config create gcs googlecloudstorage service_account_file "$HOME/.config/gcloud/your-key.json" bucket_policy_only true
  ```

Run the backup once:

```bash
./scripts/backup-to-drive.sh
```

Install the launchd agent, which runs the backup every Sunday at 06:15, after
the weekly sync:

```bash
./scripts/install-backup-scheduler.sh
```

Remove the agent:

```bash
./scripts/install-backup-scheduler.sh --remove
```

To restore, copy back from the bucket with `rclone copy`:

```bash
rclone copy gcs:your-gcs-bucket-name ./data/ --fast-list
# or, with env vars set:
rclone copy "$XDIGEST_BACKUP_REMOTE:$XDIGEST_BACKUP_BUCKET" ./data/ --fast-list
```

See `tech-context.md`, section 14.2 for the backup details.

## Scope Boundaries

The current version does not include:

- X write operations.
- LLM-based collection or Brave digest delivery.
- PDF text extraction, general crawling, or full-text discovery outside PMC.
- X data-export archive import.
- A web interface.
- Multiple X accounts.

## Develop

```bash
uv run pytest -q
uv run ruff check src tests
```

## Further Reading

- `tech-context.md`: architecture, configuration reference, X API cost record,
  log details.
