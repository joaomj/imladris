# Imladris

Imladris is a local personal knowledge base for X bookmarks and the Brave
Reading List. This guide covers installation and the first collection run.

The Python package and CLI are named `x-digest`. Settings use the `XDIGEST_`
prefix. Existing archive paths, Keychain entries, and scheduler labels do not
change with the repository name.

## Requirements

- macOS
- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- For X: an X Developer application with OAuth 2.0 PKCE enabled.
- For Brave: a local Brave profile and [Donsetch](https://github.com/dondai44423/donsetch).

You can configure either source or both. Brave collection does not require X
authorization.

## Install

```bash
git clone https://github.com/joaomj/imladris.git
cd imladris
uv sync
cp .env.example .env
```

Keep `.env` and the local `data/` archive private. Both are ignored by Git.
The default archive is in `data/`. To use another directory, set
`XDIGEST_VAULT_PATH` in `.env` before collection.

Run all commands below from the repository root.

## Set up X bookmarks

Skip this section for Brave-only use.

1. Enable bookmark, post, and user read permissions in the X Developer Console.
2. Register this OAuth redirect URI:

   ```text
   http://localhost:8080/callback
   ```

3. Set your application client ID in `.env`:

   ```text
   XDIGEST_X_CLIENT_ID=your-client-id
   ```

   Set `XDIGEST_X_CLIENT_SECRET` only if your X application requires one.

4. Start authorization:

   ```bash
   uv run x-digest auth
   ```

5. Open the printed URL and authorize the application. Submit the complete
   callback URL from the browser address bar:

   ```bash
   uv run x-digest auth --callback-url 'http://localhost:8080/callback?code=...&state=...'
   ```

The OAuth token is stored in the macOS Keychain. Later commands reuse it.

Before collecting, configure any folder or account exclusions described in
[Sync bookmarks](docs/usage.md#sync-bookmarks). Then run the first collection:

```bash
uv run x-digest sync
```

## Set up the Brave Reading List

Install [Donsetch](https://github.com/dondai44423/donsetch), then install the
optional Brave dependency:

```bash
uv sync --extra brave
```

The collector checks `PATH`, then `~/.local/bin/donsetch`. To use another
executable, set `XDIGEST_DONSETCH_BIN` to its path in `.env`.

Allow the process running the command to read Brave's profile on macOS.
Brave can remain open; collection does not change its entries or read status.

```bash
uv run --extra brave x-digest brave-sync --profile Default
```

Replace `Default` with your profile name, or set `XDIGEST_BRAVE_PROFILE` in
`.env`. See [Reading List collection](docs/usage.md#archive-the-brave-reading-list)
for source limitations and troubleshooting context.

## Verify the first collection

```bash
uv run x-digest verify --full
```

## Optional setup

- [Weekly collection schedule](docs/operations.md#automated-weekly-sync)
- [Telegram digest](docs/operations.md#weekly-telegram-digest)
- [Google Cloud Storage backup](docs/operations.md#backup-to-google-cloud-storage-free-tier)

## Documentation

- [Usage](docs/usage.md): collection, search, export, and maintenance commands.
- [Operations](docs/operations.md): scheduling, notifications, backup, and restore.
- [Technical reference](docs/tech-context.md): architecture, configuration,
  data model, development, and troubleshooting.
