"""Safe storage-path handling for the local vault."""

from pathlib import Path


def vault_root(path: Path) -> Path:
    """Return a normalized absolute vault path."""
    return path.expanduser().resolve()


def stored_path(vault_path: Path, path: Path) -> str:
    """Return a vault-relative path for persistence."""
    root = vault_root(vault_path)
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"path is outside the vault: {path}")
    return resolved.relative_to(root).as_posix()


def resolve_stored_path(vault_path: Path, value: str) -> Path:
    """Resolve a current or legacy persisted path inside the active vault."""
    root = vault_root(vault_path)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        resolved = (root / candidate).resolve()
    else:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            parts = resolved.parts
            try:
                bronze_index = parts.index("bronze")
            except ValueError as error:
                raise ValueError(f"stored path is outside the vault: {value}") from error
            resolved = root.joinpath(*parts[bronze_index:]).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"stored path is outside the vault: {value}")
    return resolved
