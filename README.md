# X Digest

X Digest keeps a private, local copy of your X bookmarks. It reads data from
the official X API, stores the raw responses and media, and builds a searchable
SQLite catalog.

The current version does not write posts, generate summaries, or use an LLM.

## Key Capabilities

- Private archive in the local `data/` directory.
- Incremental bookmark sync.
- Bookmark folder archive with an ignore list.
- One Markdown file per archived post.
- Search, export, verify, and rebuild commands.

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

## Configuration

The complete settings list is in `tech-context.md`, section 7. Common `.env`
settings:

| Setting | Purpose | Default |
| --- | --- | --- |
| `XDIGEST_X_CLIENT_ID` | X Developer application client ID | None |
| `XDIGEST_X_CLIENT_SECRET` | Optional X client secret | None |
| `XDIGEST_X_REDIRECT_URI` | OAuth callback URI | `http://localhost:8080/callback` |
| `XDIGEST_IGNORE_FOLDERS` | Comma-separated folder names or IDs to skip | empty |
| `XDIGEST_FOLDER_SYNC_DAYS` | Minimum days between folder reads | `7` |
| `XDIGEST_VAULT_PATH` | Vault location | `<project-root>/data` |
| `XDIGEST_LOG_LEVEL` | Log level | `info` |

## Local Storage

```text
<project-root>/data/
├── bronze/              # immutable API responses and media
├── silver.sqlite        # normalized records and search index
├── markdown/            # one Markdown file per archived post
└── logs/                # aggregate and per-run logs
```

The project is self-contained. Move the entire `data/` directory to relocate
everything.

## Automated Weekly Sync

Install the launchd agent, which runs `x-digest sync` every Sunday at 06:00:

```bash
./scripts/install-scheduler.sh
```

Remove the agent:

```bash
./scripts/install-scheduler.sh --remove
```

Trigger the first run immediately:

```bash
launchctl kickstart "gui/$(id -u)/com.x-digest.sync"
```

See `tech-context.md`, section 17 for the agent behavior.

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

  Credentials are read from the macOS Keychain when available. Store the
  service account JSON in the Keychain:

  ```bash
  uv run python -c "import keyring, pathlib; p=pathlib.Path.home() / '.config/gcloud/your-key.json'; keyring.set_password('x-digest','gcs-backup-credentials', p.read_text())"
  rm ~/.config/gcloud/your-key.json  # optional, keep Keychain as the only copy
  ```

  The backup script exports `RCLONE_GCS_SERVICE_ACCOUNT_CREDENTIALS` from the
  Keychain at runtime. As a fallback for one-off setups, you can create the
  `rclone` remote with a file:

  ```bash
  rclone config create gcs googlecloudstorage service_account_file $HOME/.config/gcloud/your-key.json bucket_policy_only true
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
- Summaries or LLM processing.
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
