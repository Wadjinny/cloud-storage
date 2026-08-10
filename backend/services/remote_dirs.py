"""Ensure opaque vault-dir folder chains on provider accounts."""

from __future__ import annotations

from typing import Any

from storage import folder_remotes
from storage import folders as folders_store


def ensure_remote_dir_parent(
    dir_path: str,
    account_id: str,
    provider: Any,
) -> str | None:
    """Mirror vault folders under ``dir_path`` on ``account_id``; return leaf remote id.

    Returns None when ``dir_path`` is vault root (``/``).
    """
    vault_dir_ids = folders_store.folder_id_chain(dir_path)
    if not vault_dir_ids:
        return None
    parent_remote: str | None = None
    for vault_dir_id in vault_dir_ids:
        cached = folder_remotes.get(vault_dir_id, account_id)
        if cached is not None:
            parent_remote = cached
            continue
        parent_remote = provider.ensure_child_folder(
            vault_dir_id,
            parent_id=parent_remote,
        )
        folder_remotes.put(vault_dir_id, account_id, parent_remote)
    return parent_remote
