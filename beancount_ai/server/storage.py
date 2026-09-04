import datetime
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from webdav4.client import Client  # type:ignore
from webdav4.client import ResourceNotFound as WebDAVResourceNotFound

from beancount_ai.server.config import (
    LocalFileDocumentSourcesConfiguration,
    WebDAVDocumentSourcesConfiguration,
)
from beancount_ai.structs import ItemListing

if TYPE_CHECKING:
    from beancount_ai.server.config import Configuration


class ResourceNotFoundError(FileNotFoundError):
    """Raised when a receipt file cannot be found in the configured backend."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        super().__init__(f"Receipt not found: {filename}")


class ReceiptBackend(ABC):
    """Common interface for a receipt storage backend.

    Both the WebDAV and local-file backends present this interface so the
    server commands can dispatch on the configured backend without caring
    which concrete implementation is in use.
    """

    @abstractmethod
    def list(self) -> list[ItemListing]:
        """List receipts in the configured folder as :class:`ItemListing`s."""
        raise NotImplementedError

    @abstractmethod
    def read(self, filename: str) -> bytes:
        """Return the raw bytes of *filename*, or raise ResourceNotFoundError."""
        raise NotImplementedError

    @abstractmethod
    def remove(self, filename: str) -> None:
        """Delete *filename*, or raise ResourceNotFoundError if absent."""
        raise NotImplementedError


Category = Literal["unassociated", "uningested"]


def make_receipt_backend(cfg: Configuration, category: Category) -> ReceiptBackend:
    """Return the receipt backend instance for the configured storage backend."""
    if isinstance(cfg.documents, WebDAVDocumentSourcesConfiguration):
        return WebDAVClient(cfg.documents, category)
    if isinstance(cfg.documents, LocalFileDocumentSourcesConfiguration):
        return LocalFileBackend(cfg.documents, category)
    raise ValueError(f"no receipt backend configured: {type(cfg.documents).__name__}")


class WebDAVClient(ReceiptBackend):
    def __init__(
        self,
        cfg: WebDAVDocumentSourcesConfiguration,
        category: Category,
    ):
        if category == "uningested":
            url = cfg.receipts_ingestion_url()
        else:
            url = cfg.receipts_association_url()

        self.client = Client(url, auth=(cfg.username, cfg.password))

    def list(self) -> list[ItemListing]:
        return cast(list[ItemListing], self.client.ls("/", detail=True))

    def read(self, filename: str) -> bytes:
        try:
            with self.client.open(filename, "rb") as remote_file:
                return cast(bytes, remote_file.read())
        except WebDAVResourceNotFound as e:
            raise ResourceNotFoundError(os.path.basename(filename)) from e

    def remove(self, filename: str) -> None:
        try:
            self.client.remove(filename)
        except WebDAVResourceNotFound as e:
            raise ResourceNotFoundError(os.path.basename(filename)) from e


class LocalFileBackend(ReceiptBackend):
    def __init__(self, cfg: LocalFileDocumentSourcesConfiguration, category: Category):
        self.folder = (
            cfg.receipts_uningested_folder()
            if category == "uningested"
            else cfg.receipts_unassociated_folder()
        )

    def list(self) -> list[ItemListing]:
        if not self.folder.is_dir():
            return []
        items: list[ItemListing] = []
        for entry in os.scandir(self.folder):
            if not entry.is_file():
                continue
            st = entry.stat()
            items.append(
                {
                    "name": entry.name,
                    "content_length": st.st_size,
                    "modified": datetime.datetime.fromtimestamp(
                        st.st_mtime, tz=datetime.UTC
                    ),
                }
            )
        return items

    def _path(self, filename: str) -> Path:
        return self.folder / os.path.basename(filename)

    def read(self, filename: str) -> bytes:
        path = self._path(filename)
        if not path.is_file():
            raise ResourceNotFoundError(os.path.basename(filename))
        return path.read_bytes()

    def remove(self, filename: str) -> None:
        path = self._path(filename)
        if not path.is_file():
            raise ResourceNotFoundError(os.path.basename(filename))
        path.unlink()
