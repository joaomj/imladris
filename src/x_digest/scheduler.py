"""macOS LaunchAgent installation and daily run guard."""

import json
import os
import plistlib
import subprocess
import sys
from datetime import date
from pathlib import Path

from .config import Settings
from .pipeline import Pipeline

LAUNCH_AGENT_LABEL = "com.x-digest.bookmarks-sync"


def _state_path(settings: Settings) -> Path:
    return settings.vault_path / "scheduler-state.json"


def scheduled_sync(settings: Settings) -> dict[str, str | int]:
    """Run one sync unless the local date is already complete."""
    settings.vault_path.mkdir(parents=True, exist_ok=True)
    path = _state_path(settings)
    today = date.today().isoformat()
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if state.get("last_success_date") == today:
        return {"status": "already_current", "date": today}
    result = Pipeline(settings).sync()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"last_success_date": today, "last_run_id": result["run_id"]}, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return {"status": "success", "date": today, "run_id": str(result["run_id"])}


def install_launch_agent(settings: Settings) -> Path:
    """Install and load the per-user LaunchAgent."""
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{LAUNCH_AGENT_LABEL}.plist"
    executable = Path(sys.executable).resolve()
    log_dir = settings.vault_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    environment = {"PATH": os.environ.get("PATH", "")}
    environment["XDIGEST_VAULT_PATH"] = str(settings.vault_path)
    if settings.x_client_id:
        environment["XDIGEST_X_CLIENT_ID"] = settings.x_client_id
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(executable), "-m", "x_digest.cli", "scheduled-sync"],
        "RunAtLoad": True,
        "StartInterval": settings.scheduler_interval_seconds,
        "WorkingDirectory": str(Path.cwd()),
        "EnvironmentVariables": environment,
        "StandardOutPath": str(log_dir / "launchd.stdout.log"),
        "StandardErrorPath": str(log_dir / "launchd.stderr.log"),
    }
    plist_path.write_bytes(plistlib.dumps(payload))
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip()}")
    return plist_path
