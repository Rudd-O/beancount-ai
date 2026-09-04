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
    FileBlocks,
    classify_by_target_spans,
    extract_document_paths,
    resolve_local_document_path,
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
_TARGET_REGEX = re.compile(r"^([1-9][0-9]*)(?:-([1-9][0-9]*))?$")


def parse_target(token: str) -> tuple[int, int]:
    """Parse one target token from the command line into a 1-based (start, end) pair.

    A token is either a single line number ("42" -> (42, 42), meaning the
    transaction containing that line) or an inclusive range ("12-45" ->
    (12, 45), meaning every transaction beginning between the two lines).
    """
    m = _TARGET_REGEX.match(token)
    if m is None:
        raise argparse.ArgumentTypeError(
            f"invalid target {token!r}: expected a 1-based line number (N) or an "
            "inclusive range of line numbers (A-B)"
        )
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) is not None else start
    if end < start:
        raise argparse.ArgumentTypeError(
            f"invalid target {token!r}: range end must be greater than or equal to its start"
        )
    return (start, end)


def _fmt_target(start: int, end: int) -> str:
    return f"{start}" if start == end else f"{start}-{end}"


def validate_target_ranges(
    targets: list[tuple[int, int]], n_lines: int
) -> list[tuple[int, int]]:
    """Validate user-supplied 1-based inclusive target spans; raises ValueError.

    Rules:
      - every span's line numbers must be within the file (1..n_lines);
      - spans must be strictly ascending: each subsequent span must begin
        after the previous one begins;
      - spans must not overlap: each subsequent span must begin strictly
        after the previous one ends (spans may be contiguous: 1-500 and
        501-1000 do not overlap).
    """
    if not targets:
        raise ValueError("at least one target is required")
    for idx, (start, end) in enumerate(targets, start=1):
        if start > n_lines or end > n_lines:
            raise ValueError(
                f"target range {_fmt_target(start, end)} out of file bounds "
                f"(file has {n_lines} lines)"
            )
    prev_start, prev_end = targets[0]
    for idx, (start, end) in enumerate(targets[1:], start=2):
        if start <= prev_start:
            raise ValueError(
                f"target ranges are not strictly ascending and non-overlapping: "
                f"range #{idx - 1} ({_fmt_target(prev_start, prev_end)}) and range "
                f"#{idx} ({_fmt_target(start, end)}) — a later range must begin "
                "after the earlier one"
            )
        if start <= prev_end:
            raise ValueError(
                f"target ranges are not strictly ascending and non-overlapping: "
                f"range #{idx - 1} ({_fmt_target(prev_start, prev_end)}) and range "
                f"#{idx} ({_fmt_target(start, end)}) — ranges must not overlap"
            )
    return targets


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
    """Refine existing Beancount transactions using their linked documents.

    The user points at one or more transactions by file path plus one or more
    1-based line number / line range targets (e.g. ``bean-ai refine f.bean 12
    45-80 100``).  The client extracts every targeted transaction block,
    gathers the client-local documents linked in their metadata, sends each one
    (base64-encoded) to the server over the standard transport, and asks the
    LLM for a rewritten transaction.  A colored diff is shown and, depending
    on the flags / the user's answer, the change is applied (replacing only
    the targeted transaction blocks) or discarded.
    """
    # 1. Read the file.
    tx_file = Path(args.file_path)
    if not tx_file.exists():
        print(f"Error: file not found: {tx_file}", file=sys.stderr)
        sys.exit(1)
    original_lines = tx_file.read_text(encoding="utf-8").splitlines(True)

    # 2. Validate the targets and extract the transaction blocks,
    #    preserving all formatting.  Targets are 1-based inclusive (start, end)
    #    pairs; the classifier takes zero-based spans.
    try:
        validate_target_ranges(args.targets, len(original_lines))
        spans = [(s - 1, e - 1) for s, e in args.targets]
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    blocks = classify_by_target_spans(original_lines, spans)

    def collapse_blocks_into_lines(blocks: FileBlocks) -> list[str]:
        lns: list[str] = []
        for _, _, block in blocks:
            lns.extend(block)
        return lns

    def do_refine_one(block_index: int, blocks: FileBlocks) -> None:  # noqa: C901
        """
        Attempt to refine the supplied transaction.

        If user accepts the refinement, the block corresponding to the
        block index is modified. Else nothing happens.  If the user EOFs
        or chooses to quit, we simply exit the program.
        """
        tx_block = blocks[block_index][2]
        start_lineno = blocks[block_index][1]
        if args.only_show_affected:
            pairs: list[tuple[str, str]] = []
            n = start_lineno
            for txline in tx_block:
                n = n + 1
                pairs.append((str(n), txline))
            maxlens = max(len(lnstr) for lnstr, _ in pairs)
            fmt = "%%%ds" % maxlens
            for lnstr, txline in pairs:
                sys.stdout.write(fmt % lnstr + ":" + txline)
            return
        else:
            print(
                f"Refining transaction {repr(tx_block[0].strip())} in file {tx_file}",
                file=sys.stderr,
            )

        # 3. Discover linked documents (document / documentN metadata).
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
        new_blocks[block_index] = (
            new_blocks[block_index][0],
            start_lineno,
            new_tx_block,
        )
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
            blocks[block_index] = (blocks[block_index][0], start_lineno, new_tx_block)
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
                blocks[block_index] = (
                    blocks[block_index][0],
                    start_lineno,
                    new_tx_block,
                )
                break

    for bn, (is_transaction, _, _) in enumerate(blocks):
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
        help="Refine one or more existing transactions using their linked documents",
    )
    refine_cmd.add_argument(
        "file_path",
        help="Path to the Beancount file containing the transactions to refine",
    )
    refine_cmd.add_argument(
        "targets",
        nargs="+",
        type=parse_target,
        metavar="TARGET",
        help=(
            "1-based line number of any line of a transaction to refine (N), or an "
            "inclusive range of such line numbers (A-B): every transaction beginning "
            "between the two lines is refined.  Give several targets to batch multiple "
            "edit operations in one command; they must be strictly ascending and "
            "non-overlapping."
        ),
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
    yes_group.add_argument(
        "--show-affected",
        "-s",
        action="store_true",
        default=False,
        dest="only_show_affected",
        help="Display each transaction that might be modified, then exit",
    )
    return sp
