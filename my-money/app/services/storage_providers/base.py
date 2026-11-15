"""Common storage interface.

Every provider implements the same small contract, so Uploadcare or ImageKit
(or a future provider) can be swapped by writing one new module and changing
the routing in ``app/services/storage.py`` - no other MY MONEY code changes.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


class StorageError(RuntimeError):
    """Provider failure (network, auth, quota). Message is safe to show users."""


class NotConfigured(StorageError):
    pass


@dataclass
class UploadResult:
    provider_file_id: str   # Uploadcare UUID / ImageKit fileId
    file_url: str           # storage reference stored in Turso (never shown to users)
    size: int
    content_type: str


class StorageProvider(ABC):
    name = ""

    @abstractmethod
    def configured(self) -> bool:
        """True when all credentials needed by this provider are present."""

    @abstractmethod
    def upload(self, data: bytes, filename: str, content_type: str, *, folder: str, tags=None) -> UploadResult:
        """Store bytes remotely and return the provider reference."""

    @abstractmethod
    def read(self, provider_file_id: str, file_url: str, *, content_type: str = "") -> bytes:
        """Fetch bytes server-side (used for proxying and for embedding logos in PDFs)."""

    @abstractmethod
    def delete(self, provider_file_id: str) -> None:
        """Remove the remote object."""

    def signed_url(self, provider_file_id: str, file_url: str, *, expires_in: int = 300):
        """Short-lived delivery URL, or None when the provider has no signed URLs
        (callers then proxy the bytes through Flask after the permission check)."""
        return None
