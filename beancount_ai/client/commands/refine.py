import argparse
import base64
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

from beancount_ai.client.beanfiles import (
    extract_document_paths,
    resolve_local_document_path,
    split_into_transactions_by_range,
    write_beancount_file,
)
from beancount_ai.client.config import Configuration
from beancount_ai.client.display import print_diff
from beancount_ai.client.server import (
    RemoteVM,
    demarkdownify,
    load_json,
    open_document,
    stream_reasoning_and_capture_output,
)
from beancount_ai.structs import RefineRequest, RefineRequestDocument

_TX_HEADER_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2} [*!D]\s")


def preview_local_document(doc_path: str, tx_file: Path, cfg: Configuration) -> None:
    """Open a client-local, already-linked document in the user's default viewer."""
    resolved = resolve_local_document_path(doc_path, tx_file)
    open_document(resolved)


def validate_refined_transaction(rewritten: str) -> bool:
    """Check the structural invariants a refined transaction block must satisfy.

    A valid block has a Beancount date + flag header on its first line and at
    least two posting lines (so the transaction remains well-formed).  Posting
    lines are the two-space-indented account lines (not the four-space-indented
    metadata beneath them, nor comments).
    """
    lines = rewritten.splitlines()
    if not lines or not _TX_HEADER_REGEX.match(lines[0]):
        return False
    postings = 0
    for ln in lines[1:]:
        if re.match(r"^\s\s\S", ln) and not ln.lstrip().startswith(";"):
            postings += 1
    return postings >= 2


def run(cfg: Configuration, args: argparse.Namespace) -> None:  # noqa: C901
    """Refine an existing Beancount transaction using its linked documents.

    The user points at a transaction by file path and 1-based line number.  The
    client extracts the transaction block, gathers the client-local documents
    linked in its metadata, sends them (base64-encoded) to the server over the
    standard transport, and asks the LLM for a rewritten transaction.  A colored
    diff is shown and, depending on the flags / the user's answer, the change is
    applied (replacing only the target transaction block) or discarded.
    """
    # 1. Read the file.
    tx_file = Path(args.file_path)
    if not tx_file.exists():
        print(f"Error: file not found: {tx_file}", file=sys.stderr)
        sys.exit(1)
    original_lines = tx_file.read_text(encoding="utf-8").splitlines(True)

    # 2. Extract the transaction block, preserving all formatting.
    #    The helper takes a zero-based index and accepts any line within a tx.
    try:
        blocks = split_into_transactions_by_range(
            original_lines,
            args.first_line_number - 1,
            (None if args.last_line_number is None else args.last_line_number - 1),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    def collapse_blocks_into_lines(blocks: list[tuple[bool, list[str]]]) -> list[str]:
        lns: list[str] = []
        for _, block in blocks:
            lns.extend(block)
        return lns

    def do_refine_one(block_index: int, blocks: list[tuple[bool, list[str]]]) -> None:  # noqa: C901
        """
        Attempt to refine the supplied transaction.

        If user accepts the refinement, the block corresponding to the
        block index is modified. Else nothing happens.  If the user EOFs
        or chooses to quit, we simply exit the program.
        """
        # 3. Discover linked documents (document / documentN metadata).
        tx_block = blocks[block_index][1]
        print(
            f"Refining transaction {repr(tx_block[0].strip())} in file {tx_file}",
            file=sys.stderr,
        )

        doc_paths = extract_document_paths(tx_block)

        # 4. Collect document contents (client-local, resolved near the tx file).
        documents_data: list[RefineRequestDocument] = []
        for doc_path in doc_paths:
            resolved = resolve_local_document_path(doc_path, tx_file)
            try:
                raw = resolved.read_bytes()
            except FileNotFoundError:
                print(f"Error: linked document not found: {doc_path}", file=sys.stderr)
                sys.exit(1)
            documents_data.append(
                RefineRequestDocument(
                    filepath=doc_path,
                    data=base64.b64encode(raw).decode("ascii"),
                )
            )

        # 5. Call the server — plain-JSON payload on stdin (no command argument).
        vm = RemoteVM.from_cfg(cfg)
        try:
            cmd, proc, stdin, stdout = vm.refine()
        except subprocess.CalledProcessError as e:
            raise Exception(f"Error refining receipt: {e}") from e

        accounts = cfg.beancount.account_list_file.read_text(
            encoding="utf-8"
        ).splitlines()

        request_payload: RefineRequest = {
            "transaction_text": "".join(tx_block),
            "accounts": accounts,
            "documents": documents_data,
        }
        stdin.write(json.dumps(request_payload).encode("utf-8"))
        stdin.flush()
        stdin.close()

        llm_output = stream_reasoning_and_capture_output(stdout)
        stdout.close()

        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        # 6. Parse the response — strip any markdown fences, then parse JSON.
        llm_output = demarkdownify(llm_output).strip()
        try:
            resp = load_json(llm_output)
            rewritten_tx_raw = cast(str, resp["transaction"])
        except Exception:
            print(
                "Error: could not parse LLM response as JSON. Raw output:",
                file=sys.stderr,
            )
            print(llm_output, file=sys.stderr)
            sys.exit(1)

        # We will not delete comments, either sent by the user in the original
        # transaction and returning to us, or inserted by the LLM.
        lines = rewritten_tx_raw.splitlines(True)
        rewritten_tx = "".join(lines).rstrip("\n") + "\n"

        if not validate_refined_transaction(rewritten_tx):
            print(
                "Error: LLM returned a malformed transaction (no header / "
                "fewer than two postings). Raw output:",
                file=sys.stderr,
            )
            print(llm_output, file=sys.stderr)
            sys.exit(1)

        # 7. Reassemble the file with only the target block replaced.
        new_tx_block: list[str] = [
            ln if ln.endswith("\n") else ln + "\n"
            for ln in rewritten_tx.splitlines(True)
        ]

        if args.clear and "".join(new_tx_block).strip() != "".join(tx_block).strip():
            # Transaction is different, and user requested the cleared flag be used.
            firstlnfields = new_tx_block[0].split(" ")
            firstlnfields[1] = "*"
            new_tx_block[0] = " ".join(firstlnfields)

        original_lines = collapse_blocks_into_lines(blocks)
        new_blocks = blocks[:]
        new_blocks[block_index] = (new_blocks[block_index][0], new_tx_block)
        new_lines = collapse_blocks_into_lines(new_blocks)

        diff = list(
            difflib.unified_diff(
                original_lines,
                new_lines,
                fromfile=str(tx_file),
                tofile=str(tx_file),
                n=5,
            )
        )
        if diff:
            print_diff(diff)
        else:
            print(f"No changes to {tx_file}", file=sys.stderr)
            return

        # 8. Prompt for / perform the write.
        if args.no:
            print(f"Skipping changes to {tx_file} (--no requested)", file=sys.stderr)
            return

        if args.yes:
            blocks[block_index] = (blocks[block_index][0], new_tx_block)
            return

        while True:
            print(
                f"\nApply refined transaction to {tx_file}? [y]es / [n]o / [p]review document / [q]uit ",
                file=sys.stderr,
                end="",
            )
            try:
                answer = input().strip().lower()
            except EOFError:
                sys.exit(0)
            if answer == "q":
                sys.exit(0)
            if answer == "n":
                break
            if answer == "p" and documents_data:
                preview_local_document(documents_data[0]["filepath"], tx_file, cfg)
                continue
            if answer == "y":
                blocks[block_index] = (blocks[block_index][0], new_tx_block)
                break

    for bn, (is_transaction, _) in enumerate(blocks):
        if is_transaction:
            do_refine_one(bn, blocks)

    new_lines = collapse_blocks_into_lines(blocks)

    if new_lines != original_lines:
        new_text = "".join(new_lines)
        write_beancount_file(tx_file, new_text)
        print(f"Updated transactions in {tx_file}", file=sys.stderr)


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    refine_cmd = sp.add_parser(
        "refine",
        help="Refine one or more existing transactions using its linked documents",
    )
    refine_cmd.add_argument(
        "file_path",
        help="Path to the Beancount file containing the transactions to refifne",
    )
    refine_cmd.add_argument(
        "first_line_number",
        help="line number (starts at 1) of any line of the first transaction you want to refine",
        type=int,
    )
    refine_cmd.add_argument(
        "last_line_number",
        help="line number of any line of the last transaction you want to refine",
        type=int,
        nargs="?",
        default=None,
    )
    refine_cmd.add_argument(
        "--clear",
        "-c",
        action="store_true",
        default=False,
        dest="clear",
        help="Update the flag of every modified transaction to the clear flag (*)",
    )
    yes_group = refine_cmd.add_mutually_exclusive_group()
    yes_group.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        dest="yes",
        help="Save the refinements without confirmation",
    )
    yes_group.add_argument(
        "--no",
        "-n",
        action="store_true",
        default=False,
        dest="no",
        help="Simulate and display the refinements but don't touch the transaction file",
    )
    return sp
