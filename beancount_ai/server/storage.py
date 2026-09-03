from typing import Literal, cast

from webdav4.client import Client  # type:ignore

from beancount_ai.server.config import WebDAVDocumentSourcesConfiguration
from beancount_ai.structs import ItemListing


class WebDAVClient:
    def __init__(
        self,
        cfg: WebDAVDocumentSourcesConfiguration,
        category: Literal["unassociated"] | Literal["uningested"],
    ):
        url = (
            cfg.receipts_ingestion_url()
            if category == "uningested"
            else cfg.receipts_association_url()
        )

        self.client = Client(url, auth=(cfg.username, cfg.password))

    def list(self) -> list[ItemListing]:
        return cast(list[ItemListing], self.client.ls("/", detail=True))

    def read(self, filename: str) -> bytes:
        with self.client.open(filename, "rb") as remote_file:
            return cast(bytes, remote_file.read())

    def remove(self, filename: str) -> None:
        self.client.remove(filename)
