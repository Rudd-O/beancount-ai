# Cash receipt importer — features

The geeral purpose of this software is to help the user import scanned or
photographed receipts into a Beancount accounting data set, and organize
said receipts coherently in an accessible manner.

## Ingesting receipts and creating transactions from them

Ingestion of receipts follows this procedure for each receipt the user
directs the software to handle:

* Process the receipt to create the Beancount transaction record.
* Identify the main payment account funding the transaction.
* Obtain the receipt file and store it a subfolder of the Beancount folder,
  named after the payment account.  E.g. if the payment account is
  "Assets:Cash:CHF", then the subfolder should be Assets/Cash/CHF.
* Add a `document:` metadata entry to the created Beancount transaction
  (goes right after the date line), whose value must be the full path
  of the receipt file.
* Append the created Beancount transaction to the import destination file.
* Finally, and only if all the prior steps are successful, the software will
  remove the receipt.

Interactive ingestion of receipts goes one by one, asking the user to
either preview, or ingest, or skip each receipt.

Batch ingestion of receipts imports the receipts it can, removes the ones
imported, and skips the receipts that could not be imported, leaving them
untouched instead of removing them.

## Associating receipts with existing transactions

Receipts on the server side exist that already have transactions recorded
for them on the client side (this is particularly true for transactions in
the banking Beancount file, but also true for transactions in the cash
Beancount file).

This procedure is necessary to organize these receipts, for each receipt
available to be organized:

* Process the receipt to identify its date and payment amount.
* Identify candidate transactions on the client that the receipt might
  correspond to (probably by date and payment amount, maybe with
  a bit of past/future leeway for dates).
* Evaluate the candidates to select the transaction that corresponds
  to the receipt (or perhaps a list of candidates in order of likelihood).
* Organize the receipt in the same way receipts get organized by the
  the code today.
* Add the missing `document:` metadata tag to the transaction, pointing
  to the organized receipt.

Of note: oftentimes, imported transactions already sport a `document:`
metadata tag.  It may be worthwhile to explore replacing this `document:`
tag (which usually points to an import data file) with the organized
receipt, since the receipt is often more informative than the line of
data that the import data file contains.

## Refining existing transactions

Transactions already recorded in the ledger may be incomplete or carry
simplified / inaccurate posting detail (for example, a single line item
covering the whole bill, a rough expense account, or an un-reconciled
flag).  When a receipt or other source document is already linked to such a
transaction in its `document:` / `documentN:` metadata, the `refine` command
uses that evidence to produce a more detailed and accurate version of the
same transaction.

For a transaction pointed at by file path and line number, the refinement:

* Extracts the full transaction block (date, postings, metadata, inline
  comments), leaving all surrounding data untouched.
* Reads each linked document (a local image or PDF file on disk next to
  the Beancount data).
* Sends the transaction text and the document images to the LLM together
  with the account list.
* Asks the LLM to rewrite the transaction so that it is more complete
  (adding missing line items, splitting or correcting amounts, adding
  payment forms) and more accurate (clearer narration, corrected expense
  accounts, a reconciled flag), while preserving everything already present
  in the original.
* Shows a diff and, on confirmation (or with `--yes`), rewrites only the
  target transaction block's lines in the ledger file.  `--no` shows the
  diff but modifies nothing.

The `document:` metadata tags are preserved unchanged, and the linked
document files are read-only inputs — they are never moved or deleted.
