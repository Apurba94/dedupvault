"""DedupVault: deduplicating backups."""
from .core import Vault, VaultError, chunks

__all__ = ["Vault", "VaultError", "chunks"]
