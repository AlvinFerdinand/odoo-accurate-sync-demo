# odoo-accurate-sync-demo

[![CI](https://github.com/AlvinFerdinand/odoo-accurate-sync-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/AlvinFerdinand/odoo-accurate-sync-demo/actions)

> **Demo reconstruction of a real production system.** The original moves daily
> transactions from **Odoo 19** into **Accurate Online** for **GSI Group**
> (Semarang, Indonesia). That system's code, credentials and business data belong to
> the company and are **not** published here. This repository rebuilds the sync rules
> from scratch on invented data, so the decisions can be read, run and tested in the open.

## The problem

The company runs operations in Odoo, while finance and tax reporting stay in Accurate.
Every purchase order, goods receipt, vendor bill and down payment has to exist in both,
and someone was retyping them by hand.

Copying a document once is easy. The difficult part is everything that happens after it
has been copied:

- the order is edited in Odoo after goods were already received,
- the order is cancelled,
- an older company used the same document number years ago,
- prices include VAT and carry line discounts,
- a receipt done at 18:30 UTC belongs to the next local day,
- the source ERP is briefly unavailable halfway through a run.

A sync that handles none of these makes finance clean up after it every morning.
This one is built so that people only ever have to look at a short list of cases that
genuinely need a decision.

## Rules, and why they exist

| Rule | What goes wrong without it |
|---|---|
| **The source is read-only.** There is no write method on `Source`. | One bug and the operational system is damaged, not only the copy. |
| **Next-day posting (H+1).** Documents dated today are entered tomorrow morning. | Staff edit orders several times on the day they make them, and every edit becomes a revision in accounting. |
| **Local dates from UTC.** | A late-evening receipt lands on the wrong day and the bill fails the date check. |
| **VAT-inclusive unit prices.** | The accounting app stores gross prices, so per-line conversion keeps totals equal to the source. |
| **Revisions are all or nothing.** | If one line was already received and cannot change, applying the other changes leaves a document that matches neither version. |
| **Only quantity may grow on a received line.** | Changing the price of goods already in stock rewrites their cost after the fact. |
| **Cancelled means closed, never deleted.** The number is kept and the description marked. | Deletion loses the audit trail, and a deleted number can be reused by mistake. |
| **A bill waits for its goods receipt.** | Billing before receiving creates a bill with no goods behind it. |
| **No second down payment once the final bill exists.** | The advance is counted twice. |
| **A reused number gets a dot** (`PO/0001.`), the way people did it by hand. | The new document either fails to save or overwrites the meaning of an old one. |
| **Documents entered by people are never touched.** | The sync silently undoes someone's deliberate correction. |
| **Anything that needs a person is reported, not guessed.** | A wrong number posted quietly costs more than a blank one. |

## Usage

```python
from datetime import date
from decimal import Decimal as D
from sync_engine import Line, Source, SourceOrder, SourcePicking, SyncEngine, Target, verify

source = Source(
    orders=[SourceOrder("PO/0001", "Vendor A", "2026-10-21T03:00:00",
                        (Line("CAM-1", D(2), D("1000000")), Line("CABLE", D(100), D("4000"), D(5))))],
    pickings=[SourcePicking("IN/0001", "PO/0001", "2026-10-21T03:00:00", (("CAM-1", 2),))],
)
target = Target()
result = SyncEngine(source, target, today=date(2026, 10, 22)).sync_purchase("PO/0001")

print(result.created)                       # [('order', 'PO/0001'), ('receipt', 'IN/0001')]
print(verify(source, target, "PO/0001"))    # [] -> the copy matches the source
```

`Result` separates three outcomes: `created`, `held` (waiting for something, retried on the
next run) and `manual` (needs a person; nothing was changed).

## Run the tests

```bash
pip install pytest
python -m pytest tests/ -v
```

22 tests, one or more per rule. Each rule was also checked by breaking it on purpose
(for example turning off the all-or-nothing revision, or deleting instead of closing) and
confirming that at least one test fails.

## What the production system does on top of this

- Reads the real Odoo over XML-RPC and writes through Accurate's signed REST API,
  retrying when Odoo answers 429 or 5xx.
- Handles the sales side as well: delivery orders, sales invoices and down payments.
- Runs every morning, then verifies every synced document against Odoo. On the last run,
  all 79 purchase orders matched, and payables, receivables and VAT were equal to Odoo on
  every invoice.
- Mirrors Odoo's chart of accounts in Accurate and maps each item's accounts from its Odoo
  product category, so revenue lands on the same accounts in both systems.
- Refuses to write unless the target database ID is the expected one.

Not covered here, on purpose: stock transfers, stock counts and returns, and cost-of-goods
parity. In the real setup those depend on cleaning up the item master and opening stock
first. Syncing transactions alone cannot fix them.
