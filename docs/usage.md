# Usage

Complete [setup](../README.md) first. Run these commands from the repository
root. See [Operations](operations.md) for scheduling, digests, and backups.

### Sync bookmarks

```bash
uv run x-digest sync
```

The sync is incremental: it stops as soon as a page contains only
already-archived posts. See [Incremental reads](tech-context.md#135-incremental-reads) for details.

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
[combined collection job](operations.md#automated-weekly-sync).

## Configuration

See the [configuration reference](tech-context.md#7-configuration-reference)
for the complete settings list. Common `.env`
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

## Scope Boundaries

The current version does not include:

- X write operations.
- LLM-based collection or Brave digest delivery.
- PDF text extraction, general crawling, or full-text discovery outside PMC.
- X data-export archive import.
- A web interface.
- Multiple X accounts.
