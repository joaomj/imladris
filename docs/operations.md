# Operations

Complete [setup](../README.md) and run each source manually before installing
a schedule. Run commands from the repository root.

## Automated Weekly Sync

Two mechanisms cover the two sources. A launchd agent collects X bookmarks
every Sunday at 06:00. The Brave reading list is collected from an interactive
shell, because its LevelDB store sits behind macOS Full Disk Access, which a
terminal application holds and a launchd job does not. GCS backup runs every
Sunday at 06:15, 10:15, 14:15, and 18:15, and a daily problem report at 07:00.

Brave uses `XDIGEST_BRAVE_PROFILE` or `Default`; Donsetch must be installed for
the same macOS user. The shell that runs the collection must be able to read
Brave's profile. Confirm it before relying on the schedule:

```bash
uv run --extra brave x-digest brave-sync
```

Run that once before installing. If it fails with `Operation not permitted`,
grant Full Disk Access to the terminal application you run it from in System
Settings, Privacy & Security, Full Disk Access. Granting it to the terminal is
what makes the scheduled shell run work; no launchd job reads Brave's files.

Sync the environment, then install the agents:

```bash
uv sync --extra brave
./scripts/install-scheduler.sh
./scripts/install-backup-scheduler.sh
./scripts/install-health-scheduler.sh
```

The X agent runs `x-digest sync` with the project interpreter. The backup
waits while that job runs, up to 30 minutes, and skips an attempt once that
day's copy has succeeded.

The shell trigger collects the Brave reading list from the first interactive
shell of each ISO week, after a 30-minute delay, then starts the backup. Point
it at the checkout and source the hook in `~/.zshrc`:

```sh
XDIGEST_PROJECT_DIR="/absolute/path/to/imladris"
. "$XDIGEST_PROJECT_DIR/scripts/zshrc-init.sh"
```

Keep this path in your local shell configuration. Do not edit the tracked hook
for a specific computer. The scheduler installer does not add the shell hook.
Progress lands in `data/logs/weekly-trigger.log` with a once-per-week stamp at
`data/logs/weekly-shell-trigger.stamp`. A week with no interactive shell gets
no Brave collection; the next shell that opens starts one.

Remove an agent:

```bash
./scripts/install-scheduler.sh --remove
./scripts/install-backup-scheduler.sh --remove
./scripts/install-health-scheduler.sh --remove
```

Trigger the first X run immediately:

```bash
launchctl kickstart "gui/$(id -u)/com.x-digest.sync"
```

Trigger the Brave collection and backup without waiting for a new week by
removing the stamp first:

```bash
rm data/logs/weekly-shell-trigger.stamp
./scripts/weekly-shell-trigger.sh 0
```

Both paths write one timestamp per output line, so a traceback from an earlier
run cannot be mistaken for today's. See
[Deployment and release](tech-context.md#17-deployment-and-release) for the
agent behavior.

## Daily problem report

`x-digest health` reads the database and the backup stamp and reports what went
wrong silently. It does not call X, OpenRouter, or Telegram unless you ask it
to send:

```bash
uv run x-digest health
uv run x-digest health --notify
```

It reports these findings:

| Code | Meaning |
| --- | --- |
| `run_stalled` | A run is still marked `running` past `XDIGEST_HEALTH_STUCK_RUN_MINUTES` (default 120). |
| `run_failed` | The latest run finished with status `failed`. |
| `run_missing` | The latest run started more than `XDIGEST_HEALTH_MAX_AGE_DAYS` (default 8) days ago. |
| `saved_stale` | The newest Brave reading-list read finished more than `XDIGEST_HEALTH_MAX_AGE_DAYS` days ago, so the shell trigger has not run. |
| `digest_incomplete` | The newest digest event in that window is a failure or a partial delivery, not a sent digest. |
| `backup_missing` | The backup log exists but no successful copy is recorded. |
| `backup_stale` | The last successful copy is older than `XDIGEST_HEALTH_MAX_AGE_DAYS` days. |
| `backup_timestamp_invalid` | The success stamp holds no parseable timestamp. |
| `run_timestamp_invalid` | A run row holds no parseable start time. |

The exit code is 0 when the report is clean and 1 when it lists findings.
`--notify` sends the findings to Telegram and requires the Telegram
credentials. The daily agent installed with `./scripts/install-health-scheduler.sh`
runs `health --notify` at 07:00, after the Sunday collection and backup windows.

## Weekly Telegram digest

The Sunday sync also sends one private Telegram digest after collection.
If X collection fails, the sync still attempts the next batch from archived
posts. The command continues to report the collection failure.

OpenRouter summarizes the oldest undelivered posts as themed key points with
source links. Delivery is best-effort: archive success never
depends on the digest, and failed batches stay pending for the next run. If the
model returns output that fails validation, the digest asks once more before
recording `digest_failed`. The daily problem report surfaces a digest that
never completed, so a failed week does not stay invisible.

Configure the bot and OpenRouter key in `.env`:

```text
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_USER_ID=your-chat-id
XDIGEST_LLM_API_KEY=your-openrouter-key
```

Store the OpenRouter key in Proton Pass instead of `.env`: put it in your
secrets vault as the hidden field `api-key` of the custom item
`x-digest-openrouter`, and name that vault in `.env`:

```text
XDIGEST_SECRETS_VAULT=your-vault-name
```

A set `XDIGEST_LLM_API_KEY` value takes precedence over the Proton Pass entry.
Leave `XDIGEST_SECRETS_VAULT` unset or empty to disable the lookup.

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

The application log records `token_loaded` for every run that reads the
stored token, plus `oauth_refresh_started`, `oauth_token_saved`,
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

  The backup reads the service-account JSON from Proton Pass. Store it in the
  vault named by `XDIGEST_SECRETS_VAULT` as the hidden field
  `service-account-json` of the custom item `x-digest-gcs-backup`:

  ```bash
  chmod 600 "$HOME/.config/gcloud/your-key.json"
  jq -Rs '{title: "x-digest-gcs-backup", note: "GCS service-account JSON for the monthly backup.", sections: [{section_name: "Credentials", fields: [{field_name: "service-account-json", field_type: "hidden", value: (rtrimstr("\n"))}]}]}' \
    "$HOME/.config/gcloud/your-key.json" \
    | pass-cli item create custom --vault-name your-vault-name --from-template -
  ```

  `pass-cli` runs under launchd with a minimal environment, so both x-digest
  jobs set `PROTON_PASS_KEY_PROVIDER`. Without it `pass-cli` looks for the
  session key in the OS keyring and cannot open its local database. The
  installers also put the directory that holds `pass-cli` on the job's `PATH`,
  because launchd does not inherit the interactive shell's search path.

  Verify the stored JSON without printing it:

  ```bash
  pass-cli item view 'pass://your-vault-name/x-digest-gcs-backup/service-account-json' \
    | uv run python -c "import json,sys; d=json.load(sys.stdin); print('Proton Pass OK:', d.get('type') == 'service_account')"
  ```

  The expected result is `Proton Pass OK: True`. Run the backup before you
  remove the source JSON file. After a successful backup, remove the file:

  ```bash
  ./scripts/backup-to-drive.sh
  tail -n 8 data/logs/backup.log
  rm "$HOME/.config/gcloud/your-key.json"
  ```

  The log must end with `backup end`. The script exports
  `RCLONE_GCS_SERVICE_ACCOUNT_CREDENTIALS` from Proton Pass for `rclone`, and
  stops when it cannot read the field. An already set
  `RCLONE_GCS_SERVICE_ACCOUNT_CREDENTIALS` skips the lookup.

Run the backup once:

```bash
./scripts/backup-to-drive.sh
```

Install the launchd agent, which runs the backup every Sunday at 06:15, 10:15,
14:15, and 18:15, so a long collection cannot cost a whole week. Each attempt
after a successful copy on the same day exits immediately:

```bash
./scripts/install-backup-scheduler.sh
```

Remove the agent:

```bash
./scripts/install-backup-scheduler.sh --remove
```

A successful copy writes `data/state/backup.last-success`, which both the
same-day skip and the daily problem report read. The file holds the local
completion time.

To restore, copy back from the bucket with `rclone copy`:

```bash
rclone copy gcs:your-gcs-bucket-name ./data/ --fast-list
# or, with env vars set:
rclone copy "$XDIGEST_BACKUP_REMOTE:$XDIGEST_BACKUP_BUCKET" ./data/ --fast-list
```

See [Backup and restore](tech-context.md#142-backup-and-restore) for technical
details.

## Credential failures

Both scheduled jobs read static secrets from Proton Pass. When the Proton Pass
session expires, `pass-cli` cannot open its local database and the job exits
with `pass-cli could not read pass://…` instead of continuing without
credentials. That message appears in `data/logs/backup.log` or
`data/logs/scheduler-<date>.log`, and the daily problem report names a backup
that did not run.

Renew the session in a shell that sets the same key provider the agents use,
then start the jobs again:

```bash
PROTON_PASS_KEY_PROVIDER=fs pass-cli login
launchctl kickstart -k "gui/$(id -u)/com.x-digest.sync"
launchctl kickstart -k "gui/$(id -u)/com.x-digest.backup"
```

The X OAuth token is not involved: it lives in `state_dir/oauth2.json`, so a
sync never waits on a credential dialog. When X rejects a refresh, the sync
sends a Telegram alert naming `uv run x-digest auth`.
