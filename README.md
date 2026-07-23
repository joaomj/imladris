# X Digest

A local, private, append-only archive of your X bookmarks. Keep your bookmarked
posts, long-form Articles, and media safe on your own machine, independent of X
availability.

## What it does

- Downloads your X bookmarks, including long-form Articles and attached media.
- Stores raw API responses in an immutable archive that never deletes data.
- Normalizes posts into a local searchable database.
- Runs automatically once per day when you log into your Mac.
- Lets you search, browse, and export your archive from the command line.

## Quick start

WARNING: This project is in planning stage. The steps below show the intended
workflow. Not all features are implemented.

### Install

```bash
git clone https://github.com/YOUR_USER/x-digest.git
cd x-digest
uv sync
```

### Authenticate with X

You need an X Developer account and a registered OAuth 2.0 application with
these scopes:

- `bookmark.read`
- `tweet.read`
- `users.read`
- `offline.access`

Set the redirect URI to `http://localhost:8080/callback`.

Then authorize the CLI:

```bash
uv run x-digest auth
```

This opens a browser, asks you to authorize the app, and stores your token
securely in the macOS Keychain.

### Sync your bookmarks

```bash
uv run x-digest sync
```

The first sync fetches all your bookmarked posts and saves them locally. Later
syncs only add new or changed content.

### Search your archive

```bash
uv run x-digest search "machine learning"
uv run x-digest show <post-id>
```

### Set up daily automatic sync

```bash
uv run x-digest install-scheduler
```

This installs a macOS LaunchAgent that runs `sync` when you log in, once per
day.

## How your data is stored

Everything lives in `~/Library/Application Support/x-digest/`:

- **Bronze** — raw, compressed X API responses and downloaded media files.
  Never modified or deleted.
- **Silver** — a SQLite database with normalized post records, authors,
  folders, and media metadata.
- **Gold** — full-text search index and Markdown/JSON exports.

Your OAuth token stays in the macOS Keychain. It never appears in files or
logs.

## Requirements

- macOS
- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- X Developer account with a registered OAuth 2.0 application

## Commands

| Command | Purpose |
|---------|---------|
| `auth` | Authorize the app with X |
| `sync` | Fetch and store new bookmarks |
| `status` | Show vault statistics |
| `search <q>` | Full-text search |
| `show <id>` | Display a single post |
| `export --format markdown|json` | Export your archive |
| `verify` | Check vault integrity |
| `rebuild-silver` | Rebuild database from Bronze |
| `install-scheduler` | Set up daily automatic sync |

## License

Private.
