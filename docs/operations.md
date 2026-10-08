# Operations

Complete [setup](../README.md) and run each source manually before installing
a schedule. Run commands from the repository root.

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

The optional shell trigger starts collection from the first interactive shell
each ISO week, after a 30-minute delay. It starts backup after collection ends. Before enabling the shell hook, set `_XDIGEST_TRIGGER` in
`scripts/zshrc-init.sh` to your checkout’s `scripts/weekly-shell-trigger.sh` path.
Then source `scripts/zshrc-init.sh` from `~/.zshrc`. The scheduler installer
does not add this hook.
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

See [Deployment and release](tech-context.md#17-deployment-and-release) for
the agent behavior.

## Weekly Telegram digest

The Sunday sync also sends one private Telegram digest after collection.
If X collection fails, the sync still attempts the next batch from archived
posts. The command continues to report the collection failure.

OpenRouter summarizes the oldest undelivered posts as themed key points with
source links. Delivery is best-effort: archive success never
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

### X authentication failures

If X rejects credential refresh or an API request returns HTTP 401, a normal
unbounded sync sends a Telegram alert. The alert includes the authorization
command. Alerts require Telegram credentials but do not require OpenRouter.
Bounded syncs and dry runs do not send alerts or digests.

Alert delivery uses one attempt to avoid duplicate alerts after an uncertain
network response. If alert delivery fails, the sync records the failure and
still attempts the archived digest. The command reports failure in either case.

Token refresh and login callback saves use the same process lock. If a sync
holds the lock, complete authorization after the sync ends. Start authorization
again if the callback code expires while you wait.

The application log records `oauth_refresh_started`, `oauth_token_saved`,
`oauth_refresh_failed`, and `oauth_token_save_failed`. Successful saves include
the access-token expiry time. Refresh saves also record whether the refresh
token changed. These events do not include tokens, authorization codes, or
callback URLs.

Inspect these events in `data/logs/application.jsonl` or the corresponding
file under `data/logs/runs/`. A refresh attempt without a saved-token event
can indicate an interrupted refresh. The events cannot prove whether X issued
a replacement before the interruption. X can also revoke access; local locking
cannot prevent that action.

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

See [Backup and restore](tech-context.md#142-backup-and-restore) for technical
details.
