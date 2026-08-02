#!/usr/bin/env python3
"""pycash-client — CLI wrapper that talks to pycash-server on `pim` via qrexec.

Subcommands (mirror those of pycash-server):
    list    Get the remote receipt file list as plain text (one file per line)

Config is read from ~/.config/pycash.json.
"""

import difflib
import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import ClassVar, IO, Literal, cast
import pprint

from pycash_client.beancount_loader import load_transaction_contexts, MatchResults


CONF_DEFAULT = Path.home() / ".config" / "pycash.json"


# -- configuration ---------------------------------------------------------


def demarkdownify(llm_output: str) -> str:
    llm_output_lines = llm_output.splitlines(True)
    if llm_output_lines[0].startswith("```"):
        llm_output_lines = llm_output_lines[1:]
    if llm_output_lines[-1].startswith("```"):
        llm_output_lines = llm_output_lines[:-1]
    return "".join(llm_output_lines)


class Configuration:
    """Configuration loaded from a pycash JSON config file.

    Singleton that caches its first loaded instance at the class level.
    Use :meth:`load` to retrieve or initialise it.

    Attributes:
        target_vm: Name of the Qubes VM where pycash-server runs (None to spawn locally).
        beancount_folder: Root directory of the Beancount project.
        beancount_main_file: Path to the main Beancount ledger file relative to ``beancount_folder``.
        beancount_transaction_destination_file: Filename to append ingested transactions to.
    """

    instance: ClassVar["Configuration | None"] = None
    cfg_path: ClassVar[Path | None] = None  # which file was actually loaded
    target_vm: str | None
    beancount_folder: Path
    beancount_main_file: str
    beancount_transaction_destination_file: str

    def __init__(self) -> None:
        raise NotImplementedError("Use Configuration.load() to obtain an instance")

    @classmethod
    def _get_cfg_path(cls, override: str | None) -> Path:
        """Return the config file path, resolving overrides in order of priority.

        Priority (highest → lowest):
            1. ``--config`` CLI argument
            2. ``PYCASH_CONFIG`` environment variable
            3. Default ``~/.config/pycash.json``
        """
        if override:
            return Path(override)
        env_cfg = os.environ.get("PYCASH_CONFIG")
        if env_cfg:
            return Path(env_cfg)
        return CONF_DEFAULT

    @classmethod
    def load(cls, override: str | None = None) -> "Configuration":
        """Load and cache the config from the resolved path.

        If called multiple times, only the *first* invocation's resolution is used;
        subsequent calls return the cached result (prevents a user from accidentally
        reloading with different paths within one process).
        """
        if cls.instance is not None:
            return cls.instance

        fp = cls._get_cfg_path(override)
        cls.cfg_path = fp
        with open(fp) as fh:
            data = json.load(fh)
        instance = cls.__new__(cls)
        instance.target_vm = data["target_vm"]
        instance.beancount_folder = Path(data["beancount_folder"])
        instance.beancount_main_file = data["beancount_main_file"]
        instance.beancount_transaction_destination_file = data[
            "beancount_transaction_destination_file"
        ]
        cls.instance = instance
        return cls.instance

    def transaction_destination_path(self) -> Path:
        return self.beancount_folder / os.path.basename(
            self.beancount_transaction_destination_file
        )


def shorten_fn(folder: str | Path, fn: str):
    """Reduce max path length without affecting the file name extension."""
    maxlen = os.pathconf(folder, "PC_NAME_MAX")
    # Sarn, we only handle UTF-8 file systems.  Maybe this would be good to fix in the future.
    while len(fn.encode("utf-8")) > maxlen:
        n, e = os.path.splitext(fn)
        n = n[:-1]
        fn = n + e
    return fn


# -- qrexec transport ------------------------------------------------------

# From inside a VM, IPC to another VM uses:
#   qrexec-client-vm <target_vm> <action_name> [rpc_client] [args...]
# Only stdin / stdout are relayed between client and server — the RPC action name
# determines *which* program on the target VM is invoked (registered via dom0 policy).


def stream_reasoning_and_capture_output(stdout: IO[bytes]) -> str:
    accumulated: list[str] = []

    reasoning_over = False
    for line in stdout:
        msg = json.loads(line)

        if err := msg.get("error"):
            print(err, file=sys.stderr)
            break
        elif msg.get("finish"):
            break
        elif msg.get("reasoning"):
            sys.stderr.write(msg["reasoning"])
            sys.stderr.flush()
        elif msg.get("output"):
            if not reasoning_over:
                sys.stderr.write("\n")
                sys.stderr.flush()
                reasoning_over = True
            accumulated.append(msg["output"])
        else:
            assert 0, msg

    return "".join(accumulated).strip()


class RemoteVM:
    def __init__(self, target_vm: str | None):
        self.target_vm = target_vm

    @classmethod
    def from_cfg(cls, cfg: Configuration) -> "RemoteVM":
        return cls(cfg.target_vm)

    def _call(
        self,
        action: str,
        arg: str | None = None,
    ) -> tuple[list[str], subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
        """Start a remote process and return its Popen handle (with all streams already connected)."""
        if arg is not None:
            # arguments must be hex
            arg = arg.encode("utf-8").hex()

        # Local fallback for testing: when target_vm is None, invoke pycash-server directly.
        if self.target_vm is None:
            cmd = ["pycash-server", "--config", str(Configuration.cfg_path)]
            if arg is not None:
                cmd.extend([action, arg])
            else:
                cmd.append(action)
        else:
            if arg is not None:
                action = f"{action}+{arg}"
            cmd = ["qrexec-client-vm", str(self.target_vm), action]

        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        assert proc.stdin is not None
        assert proc.stdout is not None
        return cmd, proc, proc.stdin, proc.stdout

    def list_receipts(
        self, category: Literal["unassociated"] | Literal["uningested"]
    ) -> list[str]:
        """Return receipt filenames from the server.

        Raises on qrexec transport error; prints to stderr and returns ``[]``
        when the JSON cannot be decoded.
        """

        cmd, proc, stdin, stdout = self._call(
            "pycash.List"
            + ("Uningested" if category == "uningested" else "Unassociated")
        )
        stdin.close()

        read_data = stdout.read()
        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        try:
            data = json.loads(read_data)
            mm = [os.path.basename(x) for x in data["receipts"]]
            assert mm == data["receipts"]
            return data["receipts"]
        except Exception as e:
            print(f"cannot decode server response: {e}", file=sys.stderr)
            return []

    def help_associate_receipt(
        self, filename: str
    ) -> tuple[list[str], subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
        """
        Calls upon the LLM on the server side to produce a Beancount transaction
        and the main payment account.
        """
        cmd, proc, stdin, stdout = self._call(
            "pycash.HelpAssociateReceipt", arg=filename
        )
        # FIXME caller of this rawdogs it, but the comms logic should be encapsulated in a class later.
        return cmd, proc, stdin, stdout

    def process_receipt(self, filename: str) -> tuple[str, str]:
        """
        Calls upon the LLM on the server side to produce a Beancount transaction
        and the main payment account.
        """
        cmd, proc, stdin, stdout = self._call("pycash.Process", arg=filename)
        stdin.close()

        llm_output = stream_reasoning_and_capture_output(stdout)
        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        llm_output_original = llm_output

        # Remove Markdown quote formatting from JSON output.
        llm_output = demarkdownify(llm_output)

        # Fish out first account in the payment accounts list.
        try:
            data = json.loads(llm_output)
        except json.decoder.JSONDecodeError as e:
            raise Exception(
                f"Failed decoding expected JSON at end of string: {e}\n{llm_output_original}"
            )

        try:
            payment_account = data["payment_accounts"][0]
        except Exception as e:
            raise Exception(
                f"Could not retrieve expense account from LLM output: {e}\n{llm_output_original}"
            )

        try:
            transaction = data["transaction"]
        except Exception as e:
            raise Exception(
                f"Could not retrieve Beancount transaction from LLM output: {e}\n{llm_output_original}"
            )

        return transaction, payment_account

    def fetch_receipt(self, filename: str) -> bytes:
        cmd, proc, stdin, stdout = self._call("pycash.Fetch", arg=filename)
        stdin.close()

        raw = stdout.read()
        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        return raw

    def remove_receipt(self, filename: str) -> None:
        cmd, proc, stdin, _ = self._call("pycash.Remove", arg=filename)
        stdin.close()

        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)


def predict_receipt_destination_path(
    beancount_folder: Path,
    transaction_date: date,
    filename: str,
    account: str,
    description: str | None = None,
) -> Path:
    """
    Organize a receipt file into an account folder.

    For receipts to be recognized as documents in Beancount, their filename has
    the requirement that it must begin with a date in Y-m-d format.  Hence
    the requisite transaction date at the beginning of the file name.
    """
    # Construct the final destination folder.  Account folders reside directly under `beancount_folder`.
    receipt_dir = beancount_folder / account.replace(":", "/")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    if description:
        fn = transaction_date.strftime("%Y-%m-%d.") + description + " — " + filename
    else:
        fn = transaction_date.strftime("%Y-%m-%d.") + filename

    # No slashes in the file name, please.
    fn = fn.replace("/", "_")

    fn = shorten_fn(receipt_dir, fn)

    receipt_path = receipt_dir / fn

    return receipt_path


def organize_receipt(
    beancount_folder: Path,
    vm: RemoteVM,
    transaction_date: date,
    filename: str,
    account: str,
    description: str | None = None,
) -> Path:
    """
    Organize a receipt file into an account folder.

    For receipts to be recognized as documents in Beancount, their filename has
    the requirement that it must begin with a date in Y-m-d format.  Hence
    the requisite transaction date at the beginning of the file name.
    """
    receipt_path = predict_receipt_destination_path(
        beancount_folder, transaction_date, filename, account, description
    )
    raw = vm.fetch_receipt(filename)
    receipt_path.write_bytes(raw)
    return receipt_path


def insert_document_metadata(transaction_text: str, file_path: str) -> str:
    """
    Takes a Beancount transaction and inserts the `file_path` as a document metadata entry.
    """
    lines = transaction_text.splitlines(True)
    if not lines or lines[0].strip().startswith("#"):
        return transaction_text
    stripped = lines[1].lstrip()
    indent = lines[1][: len(lines[1]) - len(stripped)]
    lines.insert(1, '{}document: "{}"\n'.format(indent, file_path.replace('"', '\\"')))
    return "".join(lines)


# Use numbered document keys (document, document2, document3, ...) so each associated
# receipt gets its own metadata key and the newest one is always just `document:`.
def update_document_metadata(line_no: int, tx_lines: list[str], new_doc: str) -> str:
    """Add or replace a document metadata entry after the date line.

    The newest doc is always ``document:`` (first in the block).  Any existing
    ``document:`` / ``documentN:`` entries are preserved but renumbered to
    ``document2:``, ``document3:``, … -- old numbering is ignored.
    """
    import re

    metadata_start = line_no  # first index AFTER the date/payee line
    assert metadata_start < len(tx_lines)

    # 1. Keep lines before metadata_start unchanged.
    result: list[str] = [line for line in tx_lines[:metadata_start]]

    # 2. Scan from metadata_start collecting doc entries; stop at empty line or non-doc.
    pre_indent = (re.match(r"^(\s+)", result[-1]) or re.match(r"^(\s+)", "  "))
    pre_str = pre_indent.group(1) if pre_indent else "  "

    doc_entries: list[tuple[int, str]] = []  # (original_line_index, path)
    j = metadata_start
    while j < len(tx_lines):
        ln = tx_lines[j]
        if not ln.strip():         # blank line → stop scanning
            break
        m = re.match(r"^\s+document(\d+)?:\s*\"?([^\"\n]*)", ln)
        if not m:                  # non-doc metadata line → stop scanning
            break
        doc_entries.append((j, m.group(2)))  # captured path value
        j += 1

    # 3. Rebuild the metadata block (newest first).
    new_lines: list[str] = [f'{pre_str}document: "{new_doc}"\n']
    for pos, (_, path) in enumerate(doc_entries):
        label = f"document{str(pos + 2)}"       # document2, document3, ...
        new_lines.append(f'{pre_str}{label}: "{path}"\n')

    result.extend(new_lines)

    # 4. Append remaining lines (everything after the scanned doc block).
    if j < len(tx_lines):
        remaining = list(tx_lines[j:])
        result.extend(remaining)

    return "".join(result)


def _preview_receipt(cfg: Configuration, filename: str, preview_dir: Path) -> None:
    dest_path = preview_dir / filename
    dest_path.write_bytes(RemoteVM.from_cfg(cfg).fetch_receipt(filename))
    subprocess.Popen(
        ["xdg-open", str(dest_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class ImportResult:
    receipt_data: bytes
    transaction_text: str
    receipt_destination_path: Path
    transaction_destination_path: Path
    rollback_size: int | None = None

    def __init__(
        self,
        vm: RemoteVM,
        beancount_folder: Path,
        transaction_destination_path: Path,
        filename: str,
    ) -> None:
        receipt_data = vm.fetch_receipt(filename)

        beancount_transaction, account = vm.process_receipt(filename)
        # Strip headline comments and newlines from the transaction.
        while beancount_transaction.lstrip().startswith(";"):
            beancount_transaction = "".join(
                beancount_transaction.splitlines(True)[1:]
            ).lstrip()

        datestr, reststr = beancount_transaction.split(" ", 1)
        # Take the text after the date, remove the transaction flag and the space next to it,
        # then use the payee and narration to construct a description for the receipt file name.
        # If there is a comment at the end of the line, strip it too.
        reststr = (
            reststr.splitlines()[0][2:]
            .replace('" "', " — ")
            .replace('"', "")
            .split(";")[0]
            .strip()
        )
        # Take the text containing the date, and make a date for the receipt file name.
        transdate = date.strptime(datestr, "%Y-%m-%d")  # type: ignore

        receipt_path = predict_receipt_destination_path(
            beancount_folder, transdate, filename, account, reststr
        )
        formatted_tx = insert_document_metadata(
            beancount_transaction, str(receipt_path)
        )

        self.receipt_data = receipt_data
        self.transaction_text = formatted_tx
        self.receipt_destination_path = receipt_path
        self.transaction_destination_path = transaction_destination_path

    def commit(self) -> None:
        dest = self.transaction_destination_path
        receipt_path = self.receipt_destination_path

        # First, write to the transaction ledger.
        with open(self.transaction_destination_path, "a") as f:
            self.rollback_size = self.transaction_destination_path.stat().st_size
            # no marker line is necessary, the transaction has a link to the document in it.
            # f.write("\n; {} imported by pycash.\n".format(args.filename))
            f.write("\n\n" + self.transaction_text.strip() + "\n")
            f.flush()

        # Then write the receipt data.
        try:
            receipt_path.write_bytes(self.receipt_data)
        except Exception:
            print(
                f"Receipt {receipt_path} could not be saved, rolling back transaction...",
                file=sys.stderr,
            )
            self.rollback()
            raise

        print(
            f"The transaction has been imported to {dest} and the receipt has been filed under {receipt_path}",
            file=sys.stderr,
        )

    def rollback(self) -> None:
        assert self.rollback_size is not None
        eee: Exception | None = None

        if self.receipt_destination_path.exists():
            try:
                self.receipt_destination_path.unlink()
            except Exception as e:
                eee = e
                print(
                    f"The receipt {self.receipt_destination_path} could not be deleted as part of the transaction rollback",
                    file=sys.stderr,
                )
        try:
            os.truncate(self.transaction_destination_path, self.rollback_size)
            self.rollback_size = None
        except Exception as e:
            eee = e
            print(
                f"The transaction written to {self.transaction_destination_path} could not be rolled back",
                file=sys.stderr,
            )

        if eee is not None:
            raise eee


def do_list_uningested(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Lists uningested receipt files from the server.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    for fname in RemoteVM.from_cfg(cfg).list_receipts("uningested"):
        print(fname)


def do_list_unassociated(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Lists receipt files yet to be associated to transactions from the server.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    for fname in RemoteVM.from_cfg(cfg).list_receipts("unassociated"):
        print(fname)


def do_fetch(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Fetches a receipt file from the server and saves it to the file specified in the arguments.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    gotten = RemoteVM.from_cfg(cfg).fetch_receipt(args.filename)
    Path(args.destination).write_bytes(gotten)


def do_remove(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Removes a receipt file from the server.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    RemoteVM.from_cfg(cfg).remove_receipt(args.filename)


def do_process(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Processes a receipt file and produces the output of the LLM to stdout.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    try:
        llm_output, account = RemoteVM.from_cfg(cfg).process_receipt(args.filename)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)

    print(llm_output)
    print(f"Main account: {account}")


def do_import(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Imports a receipt by creating a Beancount transaction for it, copying the document to
    the appropriate Beancount document folder, then writing the Beancount transaction to
    the designated transactions file while associating the transaction with the document.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    result = ImportResult(
        RemoteVM.from_cfg(cfg),
        cfg.beancount_folder,
        cfg.transaction_destination_path(),
        args.filename,
    )
    result.commit()


def do_ingest(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Processes all known receipts using the following procedure for each receipt:

    Imports the receipt from the server then, on success, deletes the receipt from the server.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    batch_mode: bool = args.batch

    vm = RemoteVM.from_cfg(cfg)
    receipts = vm.list_receipts("uningested")
    if not receipts:
        print("No receipts to ingest.", file=sys.stderr)
        return

    # Used to signal potential non-zero in batch mode.
    retval = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        preview_dir = Path(tmpdir)

        for receipt in receipts:
            if not batch_mode:
                action = "skip"
                while True:
                    print(f"\nImport '{receipt}'? [y/n/p/q] ", file=sys.stderr, end="")
                    try:
                        answer = input().strip().lower()
                    except EOFError:
                        return

                    if answer == "q":
                        return

                    if answer == "p":
                        _preview_receipt(cfg, receipt, preview_dir)
                        continue  # re-prompt for the same receipt

                    if answer == "y":
                        action = "import"

                    break  # leave prompt loop after y or n

                if action != "import":
                    continue  # genuinely skip this receipt

            # Attempt the import.
            try:
                imp = ImportResult(
                    vm,
                    cfg.beancount_folder,
                    cfg.transaction_destination_path(),
                    receipt,
                )
            except Exception as e:
                print(f"Import of {receipt} failed: {e}", file=sys.stderr)
                if batch_mode:  # defer error exit to later
                    retval = 1
                    continue
                else:
                    sys.exit(1)

            # Commit the successful import.
            try:
                imp.commit()
            except Exception as e:
                print(f"Commit of imported {receipt} failed: {e}", file=sys.stderr)
                if batch_mode:  # defer error exit to later
                    retval = 1
                    continue
                else:
                    sys.exit(1)

            # Remove from WebDAV only after successful import.
            try:
                do_remove(cfg, argparse.Namespace(filename=receipt))
            except Exception as e:
                print(
                    f"Could not remove {receipt} from WebDAV: {e}",
                    file=sys.stderr,
                )
                try:
                    # At this point, we have the transaction written and the receipt
                    # saved locally, but the receipt could not be deleted remotely,
                    # so it is safe to roll back without data loss.  Since the receipt
                    # is still on the server side, we can retry reimporting the same
                    # receipt later.
                    imp.rollback()
                except Exception as ee:
                    print(
                        f"Could not roll back transaction of imported {receipt}: {ee}",
                        file=sys.stderr,
                    )
                if batch_mode:
                    retval = 1  # defer error exit to later
                    continue
                else:
                    sys.exit(1)

    sys.exit(retval)


def do_organize(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Copies a receipt to the designated folder for the account under the Beancount folder.

    See `predict_receipt_destination_path` for requirements imposed on Beancount document
    file naming.
    """
    tdate = date.strptime(args.date, "%Y-%m-%d")  # type:ignore
    receipt_path = organize_receipt(
        cfg.beancount_folder, RemoteVM.from_cfg(cfg), tdate, args.filename, args.account
    )
    print("The file has been organized into", str(receipt_path), file=sys.stderr)


def find_transaction_in_file(file_path: Path, transaction_text: str) -> int | None:
    """Find the line number of *transaction_text* in *file_path*.

    Returns the 1-based line number where the transaction starts, or ``None``.
    """
    content = file_path.read_text(encoding="utf-8")
    lines = content.splitlines(True)
    target_lines = [ln for ln in transaction_text.splitlines(True) if ln.strip()]
    if not target_lines:
        return None

    first_line_stripped = target_lines[0].strip()
    for i, line in enumerate(lines):
        if i + len(target_lines) > len(lines):
            break
        if lines[i].strip() == first_line_stripped:
            # Verify the rest of the transaction matches.
            match = True
            for j, target_line in enumerate(target_lines[1:]):
                stripped_target = target_line.strip()
                if not stripped_target:
                    continue
                if i + 1 + j >= len(lines):
                    match = False
                    break
                if lines[i + 1 + j].strip() != stripped_target:
                    match = False
                    break
            if match:
                return i + 1  # 1-based
    return None


def do_associate(cfg: Configuration, args: argparse.Namespace) -> None:
    """Associate a receipt with an existing Beancount transaction.

    Flow:
      1. Process the receipt → get date + amount from LLM.
      2. Query Beancount for candidates within -1/+45 days.
      3. Send candidates to pycash.MatchCandidates on server.
      4. If unambiguous, auto-select + update document metadata.
      5. If ambiguous, present ranked list to user.
      6. Organize the receipt file.
    """
    vm = RemoteVM.from_cfg(cfg)

    # Step 1: Process the receipt via LLM (existing flow).
    try:
        cmd, proc, stdin, stdout = vm.help_associate_receipt(args.filename)
    except subprocess.CalledProcessError as e:
        print(f"Error processing receipt: {e}", file=sys.stderr)
        sys.exit(1)

    llm_output = demarkdownify(stream_reasoning_and_capture_output(stdout))
    receipt_info = json.loads(llm_output)
    try:
        receipt_date = date.strptime(receipt_info["date"], "%Y-%m-%d")  # type:ignore
    except KeyError:
        receipt_date = None
    amt_str, cur = receipt_info["amount"].split(" ")

    if receipt_date:
        print(f"Receipt date: {receipt_date.isoformat()}", file=sys.stderr)
    else:
        print("No date in receipt", file=sys.stderr)
    print(f"Receipt amount: {amt_str} {cur}", file=sys.stderr)

    # Step 2: Get candidates from Beancount.
    main_file = cfg.beancount_folder / cfg.beancount_main_file
    if receipt_date:
        start_date = receipt_date - timedelta(days=1)
        end_date = receipt_date + timedelta(
            days=45
        )  # 45 days ought to be good for receipts paid up to a month later

        try:
            _, contexts = load_transaction_contexts(
                str(main_file), start_date, end_date
            )
        except Exception as e:
            print(f"Error loading candidates from Beancount: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        assert 0, "Date for receipt could not be deduced."

    print(f"Found {len(contexts)} candidate transactions.", file=sys.stderr)

    candidates_data = [
        ctx.__dict__ if hasattr(ctx, "__dict__") else ctx for ctx in contexts
    ]
    # Write candidates JSON to the server.
    candidates_raw = json.dumps(candidates_data).encode("utf-8")
    stdin.write(candidates_raw)
    stdin.flush()
    stdin.close()

    llm_output = demarkdownify(stream_reasoning_and_capture_output(stdout))
    stdout.close()

    ret = proc.wait()
    if ret != 0:
        raise subprocess.CalledProcessError(ret, cmd)

    try:
        resp = cast(MatchResults, json.loads(llm_output))
    except Exception as e:
        print(f"could not decode LLM output to JSON: {e}", file=sys.stderr)
        print(f"LLM output:\n{llm_output}", file=sys.stderr)
        sys.exit(1)

    matches = resp.get("matches", [])
    if not matches:
        print("No valid matches found.", file=sys.stderr)
        return

    # Step 4 & 5: Interpret results.
    is_ambiguous = resp.get("ambiguous", False) or (
        len(matches) > 0 and matches[0].get("score", 0) < 0.8
    )

    if is_ambiguous:
        print(
            "sorry, matches are ambiguous, cannot proceed; list of matches:",
            file=sys.stderr,
        )
        return

        # Present ranked list to user.
        print("\nRanked candidates (select by index):", file=sys.stderr)
        for candidate_match in matches[:5]:  # show top 5
            print("candidate:", file=sys.stderr)
            print(pprint.pformat(candidate_match), file=sys.stderr)
        #     idx = candidate_match["index"]
        #     score = candidate_match.get("score", 0)
        #     ctx = contexts[idx]

        #     amount_str = (
        #         f"{ctx['paid_amount']} {ctx['paid_currency']}"
        #         if ctx.get("paid_amount")
        #         else "N/A"
        #     )
        #     payee = ctx.get("payee") or "N/A"
        #     narration = (ctx.get("narration") or "N/A").ljust(30)

        #     print(
        #         f"  [{idx}] score={score:.2f} {ctx['date_str']}  {str(payee).ljust(25)}  {narration}{amount_str:>15}",
        #         file=sys.stderr,
        #     )

        # while True:
        #     try:
        #         choice = input("\nSelect candidate (index, or 'r' to retry): ")
        #         if choice.strip().lower() == "r":
        #             print(
        #                 "Retrying...", file=sys.stderr
        #             )  # TODO: actually reload and re-match
        #             pass  # stay in loop

        #         selected_idx = int(choice.strip())
        #         if 0 <= selected_idx < len(contexts):
        #             is_ambiguous = False
        #             break
        #         else:
        #             print(
        #                 f"Invalid index. Use [0-{len(contexts) - 1}].", file=sys.stderr
        #             )
        #     except (ValueError, EOFError):
        #         print("Please enter a valid number.", file=sys.stderr)
        return

    selected_match_index = 0
    selected_match = matches[selected_match_index]

    selected_txes = [
        tx
        for tx in contexts
        if tx.line_no == selected_match["line_no"]
        and tx.source_file == selected_match["source_file"]
    ]

    if len(selected_txes) > 1:
        print(
            f"Multiple transactions fit selected transaction match produced by LLM: {selected_match}",
            file=sys.stderr,
        )
        sys.exit(1)
    elif not selected_txes:
        print(
            f"No transactions fit selected transaction match produced by LLM: {selected_match}",
            file=sys.stderr,
        )

    selected_tx = selected_txes[0]

    # Step 6: Download + organize receipt.
    receipt_path = predict_receipt_destination_path(
        cfg.beancount_folder, receipt_date, args.filename, selected_tx.crediting_account
    )

    # Step 7: Update document metadata.
    tx_file = Path(selected_tx.source_file)
    line_no = selected_tx.line_no

    if not tx_file.exists():
        print(
            f"Warning: Transaction source file '{tx_file}' does not exist. Cannot update metadata.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Read the transaction text and update it.
    all_lines = tx_file.read_text(encoding="utf-8").splitlines(True)

    if line_no > len(all_lines):
        print(
            f"Error: line number {line_no} exceeds file length ({len(all_lines)}).",
            file=sys.stderr,
        )
        return

    new_content = update_document_metadata(
        line_no,
        all_lines,
        str(receipt_path),
    )

    old_lines = all_lines
    new_lines_txt = (
        new_content.rstrip("\n") + "\n"
        if not new_content.endswith("\n")
        else new_content
    )
    new_lines = new_lines_txt.splitlines(True)

    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=str(tx_file),
            tofile=str(tx_file),
        )
    )
    if diff:
        print("--- Changes ---", file=sys.stdout)
        for line in diff:
            sys.stdout.write(line)

    if args.no:
        print(f"Skipping changes to {tx_file} (--no requested)", file=sys.stderr)
        return

    if args.yes:
        print(f"\nSave changes to '{tx_file}'? [y/n] ", file=sys.stderr, end="")
        try:
            answer = input().strip().lower()
        except EOFError:
            return

        if answer != "y":
            return

    # Download the receipt and save it organized.
    raw_bytes = vm.fetch_receipt(args.filename)
    receipt_path.write_bytes(raw_bytes)

    print(f"Receipt saved to {receipt_path}", file=sys.stderr)

    tx_file.write_text(new_content, encoding="utf-8")
    print(f"Updated document metadata on line {line_no} of {tx_file}", file=sys.stderr)

    vm.remove_receipt(args.filename)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pycash-client",
        description="qrexec client for pycash",
    )
    ap.add_argument(
        "--config",
        "-c",
        default=None,
        dest="conf_path",
        help="Path to the config file; overrides $PYCASH_CONFIG and the default",
    )
    sp = ap.add_subparsers(dest="command")

    sp.add_parser(
        "list-unassociated",
        help="Get receipt filenames to associate with transactions (plain text)",
    )
    sp.add_parser(
        "list-uningested",
        help="Get receipt filenames to import as transactions (plain text)",
    )

    process_cmd = sp.add_parser(
        "process", help="Process a receipt image via Open-WebUI"
    )
    process_cmd.add_argument("filename", help="Filename of the receipt")

    fetch_cmd = sp.add_parser("fetch", help="Fetch a receipt file from the server")
    fetch_cmd.add_argument("filename", help="Filename of the receipt")
    fetch_cmd.add_argument("destination", help="Local path to save the retrieved file")

    rm_cmd = sp.add_parser("remove", help="Delete a receipt file")
    rm_cmd.add_argument("filename", help="Filename of the receipt file")

    org_cmd = sp.add_parser("organize", help="File a receipt under a payment account")
    org_cmd.add_argument("filename", help="Filename of the receipt")
    org_cmd.add_argument("date", help="Date to impute to receipt file", type=str)
    org_cmd.add_argument("account", help="Payment account (e.g. Assets:Cash:CHF)")

    imp_cmd = sp.add_parser(
        "import",
        help="Partial ingest pipeline: LLM → organize → append to Beancount, but receipt in server is left alone",
    )
    imp_cmd.add_argument("filename", help="Filename of the receipt")

    ing_cmd = sp.add_parser(
        "ingest",
        help="Batch ingest receipts interactively: process → organize → append → remove",
    )
    ing_cmd.add_argument(
        "--batch",
        "-b",
        action="store_true",
        default=False,
        dest="batch",
        help="Non-interactive batch mode: import what it can, skip the rest",
    )

    assoc_cmd = sp.add_parser(
        "associate", help="Associate a receipt with an existing Beancount transaction"
    )
    assoc_cmd.add_argument("filename", help="Filename of the receipt")
    yes_group = assoc_cmd.add_mutually_exclusive_group()
    yes_group.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        dest="yes",
        help="Confirm all actions automatically (equivalent to answering 'yes' to every prompt)",
    )
    yes_group.add_argument(
        "--no",
        "-n",
        action="store_true",
        default=False,
        dest="no",
        help="Deny all actions automatically (equivalent to answering 'no' to every prompt)",
    )

    return ap


def main() -> None:
    global _cfg_override
    ap = build_parser()
    args = ap.parse_args()

    cfg = Configuration.load(args.conf_path)

    if not args.command:
        ap.print_help(sys.stderr)
        sys.exit(1)

    dispatch = {
        "list-unassociated": do_list_unassociated,
        "list-uningested": do_list_uningested,
        "fetch": do_fetch,
        "import": do_import,
        "ingest": do_ingest,
        "organize": do_organize,
        "process": do_process,
        "remove": do_remove,
        "associate": do_associate,
    }
    dispatch[args.command](cfg, args)


if __name__ == "__main__":
    main()
