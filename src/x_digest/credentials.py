"""Read static secrets from Proton Pass."""

from __future__ import annotations

import subprocess

PASS_CLI = "pass-cli"
READ_TIMEOUT_SECONDS = 60
GCS_BACKUP_ITEM = "x-digest-gcs-backup"
GCS_BACKUP_FIELD = "service-account-json"
OPENROUTER_ITEM = "x-digest-openrouter"
OPENROUTER_FIELD = "api-key"


class CredentialError(RuntimeError):
    """Raised when a secret cannot be read from Proton Pass."""


def read_field(vault: str, item: str, field: str) -> str:
    """Return one Proton Pass field value.

    `pass-cli` inherits this process environment, so the Proton Pass settings
    that make a session usable under launchd must be present there.
    """
    reference = f"pass://{vault}/{item}/{field}"
    try:
        result = subprocess.run(
            [PASS_CLI, "item", "view", reference],
            capture_output=True,
            text=True,
            timeout=READ_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as error:
        raise CredentialError(f"pass-cli is not installed; cannot read {reference}") from error
    except subprocess.TimeoutExpired as error:
        raise CredentialError(f"pass-cli timed out reading {reference}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise CredentialError(f"pass-cli could not read {reference}: {detail}")
    value = result.stdout.strip()
    if not value:
        raise CredentialError(f"Proton Pass returned an empty value for {reference}")
    return value
