import datetime
import json
from dataclasses import dataclass
from typing import IO, Any, TypedDict, cast


class BadJSON(json.decoder.JSONDecodeError):
    def __str__(self) -> str:
        return json.decoder.JSONDecodeError.__str__(self) + "\nText:\n" + (self.doc)


def load_json(s: str | bytes) -> Any:
    try:
        return json.loads(s)
    except json.decoder.JSONDecodeError as e:
        raise BadJSON(e.msg, s if isinstance(s, str) else s.decode("utf-8"), e.pos)


@dataclass
class FetchedReceipt:
    """A receipt file as returned by the server's ``beanai.Fetch`` subcommand.

    Combines the raw bytes of the document with the modification timestamp the
    server knows about, so callers can preserve it when saving the file locally.
    """

    data: bytes
    timestamp: float

    @classmethod
    def load(cls, f: IO[bytes]) -> "FetchedReceipt":
        raw = f.read()

        # The server sends one JSONL metadata line, then the raw bytes of the
        # receipt.  Partition on the first newline to separate the two.
        meta, _, data = raw.partition(b"\n")
        timestamp = cast(float, load_json(meta)["timestamp"])
        return cls(data, timestamp)


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
