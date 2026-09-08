import base64
import datetime
import json
from dataclasses import dataclass
from typing import IO, Any, NotRequired, TypedDict, cast

VALID_EXTENSIONS = frozenset((".jpg", ".jpeg", ".png", ".pdf"))


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
    """A receipt file as returned by the server's ``beanhand.Fetch`` subcommand.

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
    account carries no beanhand-rules guidance.
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


@dataclass
class ReceiptPayload:
    """A receipt relayed by the client to the AI server (wire form).

    ``content`` is the raw file bytes, base64-encoded.  The client fetches
    the receipt from the documents server and inlines it here, so the AI
    server never touches the receipt storage itself.
    """

    filename: str
    content: bytes

    def serialize(self) -> str:
        return json.dumps(self.export())

    def export(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "content": base64.b64encode(self.content).decode("ascii"),
        }

    @classmethod
    def deserialize(cls, text: str) -> ReceiptPayload:
        """Validate and decode the ``receipt`` field of a Process/HelpAssociateReceipt request.

        Returns the ``(filename, decoded raw bytes)`` pair when *data* is a JSON
        object with a non-empty string ``filename`` and a string ``content`` that
        is valid base64; otherwise raises a ``ValueError`` describing the first
        problem found.
        """
        return cls.validate(load_json(text))

    @classmethod
    def validate(cls, data: Any) -> ReceiptPayload:
        if not isinstance(data, dict):
            raise ValueError("Invalid receipt: not a dictionary")

        if "filename" not in data:
            raise ValueError("Invalid receipt: missing the filename")
        if "content" not in data:
            raise ValueError("Invalid receipt: missing the content")

        filename = data["filename"]  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(filename, str) or not filename:
            raise ValueError("Invalid receipt: filename is not a string")
        if "\n" in filename:
            raise ValueError("Invalid receipt: receipt filename contains new lines")

        content = data["content"]  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(content, str):
            raise ValueError("Invalid receipt: receipt content is not a string")

        try:
            raw = base64.b64decode(content, validate=True)
        except Exception as e:
            raise ValueError(
                f"Invalid receipt: receipt content is not valid base64: {e}"
            ) from e
        return cls(filename=filename, content=raw)


@dataclass
class ProcessRequest:
    """Payload sent to the AI server's ``beanhand.Process`` subcommand over stdin."""

    accounts: list[AccountRef]
    receipt: ReceiptPayload

    def serialize(self) -> str:
        return json.dumps(self.export())

    def export(self) -> dict[str, Any]:
        return {
            "accounts": self.accounts,
            "receipt": self.receipt.export(),
        }

    @classmethod
    def deserialize(cls, data: str) -> ProcessRequest:
        return cls.validate(load_json(data))

    @classmethod
    def validate(cls, data: Any) -> ProcessRequest:
        if not isinstance(data, dict):
            raise ValueError("Invalid process request: not a dictionary")

        if "accounts" not in data:
            raise ValueError("Invalid process request: missing accounts")
        if "receipt" not in data:
            raise ValueError("Invalid process request: missing receipt")

        try:
            account_refs = check_account_refs(data["accounts"])
        except ValueError as reason:
            raise ValueError(f"Invalid process request accounts: {reason}") from reason

        try:
            rp = ReceiptPayload.validate(data["receipt"])
        except ValueError as reason:
            raise ValueError(f"Invalid process request receipt: {reason}") from reason

        return cls(accounts=account_refs, receipt=rp)


@dataclass
class ProcessResponse:
    """Processed payload returned by ``beanhand.Process`` over stdout."""

    transaction: str
    payment_account: str

    @classmethod
    def deserialize(cls, text: str) -> ProcessResponse:
        return cls.validate(load_json(text))

    @classmethod
    def validate(cls, data: dict[Any, Any]) -> ProcessResponse:
        try:
            payment_account = data["payment_accounts"][0]
        except (KeyError, IndexError) as e:
            raise ValueError(f"Payment accounts are absent from LLM output")

        try:
            transaction = data["transaction"]
        except KeyError as e:
            raise ValueError(f"Transaction is absent from LLM output: {e}")

        return ProcessResponse(
            **{"transaction": transaction, "payment_account": payment_account}
        )


@dataclass
class AssociateRequest:
    """First stdin line of the AI server's ``beanhand.HelpAssociateReceipt`` subcommand.

    The receipt is relayed inline because the AI server has no access to the
    receipt storage.  The candidate transactions arrive as a second stdin line
    (a bare JSON array) after the first LLM pass has completed.
    """

    receipt: ReceiptPayload

    def serialize(self) -> str:
        return json.dumps(self.export())

    def export(self) -> dict[str, Any]:
        return {
            "receipt": self.receipt.export(),
        }

    @classmethod
    def deserialize(cls, text: str) -> AssociateRequest:
        return cls.validate(load_json(text))

    @classmethod
    def validate(cls, data: Any) -> AssociateRequest:
        if not isinstance(data, dict):
            raise ValueError("AssociateRequest is not a dictionary")
        if "receipt" not in data:
            raise ValueError("Receipt is absent from associate request")

        return AssociateRequest(**{"receipt": ReceiptPayload.validate(data["receipt"])})


@dataclass
class RefineRequest:
    """Payload sent to the server's ``beanhand.Refine`` subcommand over stdin."""

    transaction_text: str
    accounts: list[AccountRef]
    documents: list[ReceiptPayload]

    def serialize(self) -> str:
        return json.dumps(self.export())

    def export(self) -> dict[str, Any]:
        return {
            "transaction_text": self.transaction_text,
            "documents": [x.export() for x in self.documents],
            "accounts": self.accounts,
        }

    @classmethod
    def deserialize(cls, text: str) -> RefineRequest:
        return cls.validate(load_json(text))

    @classmethod
    def validate(cls, data: Any) -> RefineRequest:
        if not isinstance(data, dict):
            raise ValueError("RefineRequest is not a dictionary")
        if "transaction_text" not in data:
            raise ValueError("Transaction is absent from RefineRequest")
        if not isinstance(data["transaction_text"], str):
            raise ValueError("Transaction in RefineRequest is not a string")
        if "accounts" not in data:
            raise ValueError("Account list is absent from RefineRequest")
        accts = check_account_refs(data["accounts"])
        documents: list[ReceiptPayload] = []
        if "documents" in data:
            if not isinstance(data["documents"], list):
                raise ValueError("RefineRequest documents is not a list")
            for x, rp in enumerate(data["documents"]):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
                try:
                    documents.append(ReceiptPayload.validate(rp))
                except Exception as e:
                    raise ValueError(
                        f"Bad document {x + 1} in documents list of RefineRequest: {e}"
                    ) from e

        return RefineRequest(
            transaction_text=data["transaction_text"],
            accounts=accts,
            documents=documents,
        )


@dataclass
class RefineResponse:
    """Payload sent to the server's ``beanhand.Refine`` subcommand over stdin."""

    transaction: str

    def serialize(self) -> str:
        return json.dumps(self.export())

    def export(self) -> dict[str, Any]:
        return {
            "transaction": self.transaction,
        }

    @classmethod
    def deserialize(cls, text: str) -> RefineResponse:
        return cls.validate(load_json(text))

    @classmethod
    def validate(cls, data: Any) -> RefineResponse:
        if not isinstance(data, dict):
            raise ValueError("RefineResponse is not a dictionary")
        if "transaction" not in data:
            raise ValueError("Transaction is absent from RefineResponse")
        if not isinstance(data["transaction"], str):
            raise ValueError("Transaction in RefineResponse is not a string")

        return RefineResponse(
            transaction=data["transaction"],
        )


class ItemListing(TypedDict):
    name: str
    content_length: int | None
    modified: datetime.datetime
