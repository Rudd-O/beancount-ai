from typing import TypedDict
import datetime


class RefineRequestDocument(TypedDict):
    """A linked document to send to the server for a refine request."""

    filepath: str
    data: str  # base64-encoded raw bytes


class RefineRequest(TypedDict):
    """Payload sent to the server's ``beanai.Refine`` subcommand over stdin."""

    transaction_text: str
    accounts: list[str]
    documents: list[RefineRequestDocument]


class ItemListing(TypedDict):
    name: str
    content_length: int | None
    modified: datetime.datetime
