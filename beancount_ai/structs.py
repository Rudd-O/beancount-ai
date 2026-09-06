import datetime
import json
from dataclasses import dataclass
from typing import IO, Any, NotRequired, TypedDict, cast


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


class AccountRef(TypedDict):
    """One account offered to the LLM.

    ``name`` is required; ``rule`` is optional and omitted when the
    account carries no bean-ai-rules guidance.
    """

    name: str
    rule: NotRequired[str]


def check_account_refs(refs: Any) -> list[AccountRef]:
    """Validate a JSON-decoded account list (the wire shape of ``AccountRef``).

    Returns a valid list of AccountRefs when *refs* is a JSON array of objects
    each carrying a non-empty string ``name`` and, optionally, a string ``rule``;
    otherwise raises a ``ValueError`` describing the first problem found.
    """
    if not isinstance(refs, list):
        raise ValueError("expected a JSON array of {name, rule?} objects")
    rrefs = cast(list[Any], refs)  # type:ignore
    for n, ref in enumerate(rrefs):
        if not isinstance(ref, dict):
            raise ValueError(f"element {n} is not an object")
        ref = cast(dict[Any, Any], ref)  # type:ignore
        name = ref.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"element {n} is missing a string 'name'")
        if not isinstance(ref["name"], str):
            raise ValueError(f"element {n} has a non-string 'name'")
        if "\n" in ref["name"]:
            raise ValueError(f"element {n} contains a 'name' with new lines")
        if "rule" in ref and not isinstance(ref["rule"], str):
            raise ValueError(f"element {n} has a non-string 'rule'")
        if "rule" in ref and "\n" in ref["rule"]:
            raise ValueError(f"element {n} contains a 'rule' with new lines")
        keys = set(ref.keys()) - {"name", "rule"}
        if keys:
            raise ValueError(f"element {n} has keys other than 'name' and 'rule'")
    return rrefs


class RefineRequestDocument(TypedDict):
    """A linked document to send to the server for a refine request."""

    filepath: str
    data: str  # base64-encoded raw bytes


class RefineRequest(TypedDict):
    """Payload sent to the server's ``beanai.Refine`` subcommand over stdin."""

    transaction_text: str
    accounts: list[AccountRef]
    documents: list[RefineRequestDocument]


class ItemListing(TypedDict):
    name: str
    content_length: int | None
    modified: datetime.datetime
