# -*- coding: utf-8 -*-
"""A from-scratch demo of a one-way ERP-to-accounting document sync.

Clean-room rebuild of the patterns behind a production Odoo -> Accurate Online
integration. Nothing here is copied from that codebase: the "source" and "target"
below are small in-memory stand-ins with invented data, so every rule can be read
and tested in isolation.

The source (think Odoo) is only ever READ. The engine copies confirmed purchase
orders, goods receipts, vendor bills and down payments into the target (think
Accurate) the way an admin would type them in, and a verifier compares the two
afterwards. The interesting part is not the copying - it is the rules for when the
source changes after something was already posted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

VAT = Decimal("0.11")
LOCAL_TZ = timezone(timedelta(hours=7))          # documents are dated in local time (UTC+7)
CENT = Decimal("0.01")


def money(x: Decimal) -> Decimal:
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def local_date(utc_iso: str) -> date:
    """Source timestamps are UTC. A receipt done at 18:30 UTC belongs to the NEXT local day."""
    return datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).date()


# ---------------------------------------------------------------- source (read-only)
@dataclass(frozen=True)
class Line:
    product: str
    qty: Decimal
    unit_price: Decimal                 # before VAT and before discount
    discount_pct: Decimal = Decimal(0)
    taxed: bool = True

    @property
    def subtotal(self) -> Decimal:
        return money(self.qty * self.unit_price * (1 - self.discount_pct / 100))

    @property
    def total(self) -> Decimal:
        return money(self.subtotal * (1 + VAT)) if self.taxed else self.subtotal


@dataclass(frozen=True)
class SourceOrder:
    name: str
    partner: str
    ordered_utc: str
    lines: tuple
    state: str = "confirmed"            # confirmed | cancel
    changed_utc: str = ""

    @property
    def total(self) -> Decimal:
        return sum((l.total for l in self.lines), Decimal(0))


@dataclass(frozen=True)
class SourcePicking:
    name: str
    order: str
    done_utc: str
    moves: tuple                        # ((product, qty), ...)
    state: str = "done"                 # done | assigned | cancel


@dataclass(frozen=True)
class SourceInvoice:
    name: str
    order: str
    invoice_date: str                   # already a local date, YYYY-MM-DD
    amount_total: Decimal
    kind: str = "final"                 # final | down_payment


class Source:
    """Read-only view of the ERP. There is deliberately no method that writes."""

    def __init__(self, orders=(), pickings=(), invoices=()):
        self._orders = {o.name: o for o in orders}
        self._pickings = tuple(pickings)
        self._invoices = tuple(invoices)

    def order(self, name: str) -> SourceOrder:
        return self._orders[name]

    def pickings(self, order: str):
        return [p for p in self._pickings if p.order == order]

    def invoices(self, order: str):
        return [i for i in self._invoices if i.order == order]


# ---------------------------------------------------------------- target (accounting)
class ApiRejected(Exception):
    """The accounting API refused a change (it does, for several real reasons)."""


@dataclass
class TargetLine:
    product: str
    qty: Decimal
    gross_price: Decimal                # VAT-inclusive unit price, as the accounting app stores it
    discount_pct: Decimal
    taxed: bool
    processed_qty: Decimal = Decimal(0)  # already received / delivered against this line
    closed: bool = False

    @property
    def total(self) -> Decimal:
        return money(self.qty * self.gross_price * (1 - self.discount_pct / 100))


@dataclass
class TargetDoc:
    id: int
    kind: str                           # order | receipt | bill | down_payment
    number: str
    partner: str
    doc_date: date
    created_by: str
    lines: list = field(default_factory=list)
    order: str = ""
    amount: Decimal = Decimal(0)
    description: str = ""

    @property
    def total(self) -> Decimal:
        return sum((l.total for l in self.lines), Decimal(0)) if self.lines else self.amount


class Target:
    """In-memory accounting system with the constraints the real API enforces."""

    def __init__(self):
        self.docs: dict[int, TargetDoc] = {}
        self._next = 1

    def find(self, kind: str, number: str) -> Optional[TargetDoc]:
        return next((d for d in self.docs.values() if d.kind == kind and d.number == number), None)

    def by_order(self, kind: str, order: str):
        return [d for d in self.docs.values() if d.kind == kind and d.order == order]

    def create(self, **kw) -> TargetDoc:
        if self.find(kw["kind"], kw["number"]):
            raise ApiRejected("number %s already used" % kw["number"])
        doc = TargetDoc(id=self._next, **kw)
        self.docs[doc.id] = doc
        self._next += 1
        return doc

    def update_line(self, doc: TargetDoc, index: int, *, qty=None, gross_price=None, discount_pct=None):
        line = doc.lines[index]
        if qty is not None and qty < line.processed_qty:
            raise ApiRejected("quantity below what was already processed")
        if line.processed_qty and (gross_price is not None or discount_pct is not None):
            raise ApiRejected("price of a processed line cannot change")
        for name, value in (("qty", qty), ("gross_price", gross_price), ("discount_pct", discount_pct)):
            if value is not None:
                setattr(line, name, value)

    def remove_line(self, doc: TargetDoc, index: int):
        if doc.lines[index].processed_qty:
            raise ApiRejected("a processed line cannot be removed")
        del doc.lines[index]

    def close(self, doc: TargetDoc, note: str):
        for line in doc.lines:
            line.closed = True
        doc.description = (note + doc.description)[:250]

    def delete(self, doc: TargetDoc):
        del self.docs[doc.id]


# ---------------------------------------------------------------- the engine
def gross_price(line: Line) -> Decimal:
    """The target stores VAT-inclusive prices; converting per unit keeps totals equal to the source."""
    return line.unit_price * (1 + VAT) if line.taxed else line.unit_price


def free_number(target: Target, kind: str, number: str, partner: str, total: Decimal) -> Optional[str]:
    """Number to use for a new document, or None if this exact document already exists.

    Two generations of companies in the source reuse the same numbering, so a number can
    already belong to an unrelated document. A person would add a dot ("PO/0034.") - so does this.
    """
    candidate = number
    for _ in range(4):
        existing = target.find(kind, candidate)
        if existing is None:
            return candidate
        if existing.partner == partner and abs(existing.total - total) <= 1:
            return None
        candidate += "."
    raise ApiRejected("no free number for %s" % number)


@dataclass
class Result:
    created: list = field(default_factory=list)
    held: list = field(default_factory=list)       # waiting for something; will retry next run
    manual: list = field(default_factory=list)     # needs a person; nothing was changed


class SyncEngine:
    def __init__(self, source: Source, target: Target, today: date, user: str = "sync"):
        self.source, self.target, self.today, self.user = source, target, today, user

    # .......................................................... public
    def sync_purchase(self, name: str) -> Result:
        res = Result()
        order = self.source.order(name)
        if order.state == "cancel":
            self._close_cancelled(order, res)
            return res
        if local_date(order.ordered_utc) >= self.today:
            res.held.append("%s is dated today: entered tomorrow morning (H+1)" % name)
            return res
        doc = self._ensure_order(order, res)
        if doc is None:
            return res
        self._receipts(order, doc, res)
        self._invoices(order, res)
        return res

    # .......................................................... order
    def _target_lines(self, order: SourceOrder):
        return [TargetLine(l.product, l.qty, gross_price(l), l.discount_pct, l.taxed) for l in order.lines]

    def _ensure_order(self, order: SourceOrder, res: Result) -> Optional[TargetDoc]:
        doc = next((d for d in self.target.docs.values() if d.kind == "order" and d.number.rstrip(".") == order.name
                    and d.partner == order.partner), None)
        if doc is None:
            number = free_number(self.target, "order", order.name, order.partner, order.total)
            doc = self.target.create(kind="order", number=number, partner=order.partner,
                                     doc_date=local_date(order.ordered_utc), created_by=self.user,
                                     lines=self._target_lines(order), order=order.name)
            res.created.append(("order", doc.number))
            return doc
        if doc.created_by != self.user:
            return doc                                  # entered by a person: never touched
        manual = revise_order(self.target, doc, order)
        if manual:
            res.manual.extend(manual)
            return None
        return doc

    # .......................................................... receipts
    def _receipts(self, order: SourceOrder, doc: TargetDoc, res: Result):
        for p in self.source.pickings(order.name):
            moves = [(prod, Decimal(q)) for prod, q in p.moves if Decimal(q) > 0]
            if p.state != "done" or not moves or self.target.find("receipt", p.name):
                continue
            if local_date(p.done_utc) >= self.today:
                res.held.append("%s received today: entered tomorrow (H+1)" % p.name)
                continue
            lines = []
            for prod, qty in moves:
                ol = next(l for l in doc.lines if l.product == prod)
                ol.processed_qty += qty
                lines.append(TargetLine(prod, qty, ol.gross_price, ol.discount_pct, ol.taxed, processed_qty=qty))
            self.target.create(kind="receipt", number=p.name, partner=order.partner,
                               doc_date=local_date(p.done_utc), created_by=self.user, lines=lines, order=order.name)
            res.created.append(("receipt", p.name))

    # .......................................................... bills & down payments
    def _invoices(self, order: SourceOrder, res: Result):
        source_receipts = [p for p in self.source.pickings(order.name) if p.state == "done"]
        for inv in sorted(self.source.invoices(order.name), key=lambda i: i.kind != "down_payment"):
            kind = "bill" if inv.kind == "final" else "down_payment"
            if kind == "down_payment" and self.target.by_order("bill", order.name):
                continue                                # final bill exists: a second down payment would double-count
            if kind == "bill" and any(not self.target.find("receipt", p.name) for p in source_receipts):
                res.held.append("%s waits for its goods receipt" % inv.name)
                continue
            number = free_number(self.target, kind, inv.name, order.partner, inv.amount_total)
            if number is None:
                continue                                # already there
            self.target.create(kind=kind, number=number, partner=order.partner,
                               doc_date=date.fromisoformat(inv.invoice_date), created_by=self.user,
                               order=order.name, amount=inv.amount_total)
            res.created.append((kind, number))

    # .......................................................... cancellations
    def _close_cancelled(self, order: SourceOrder, res: Result):
        doc = next((d for d in self.target.by_order("order", order.name) if d.created_by == self.user), None)
        if doc is None or all(l.closed for l in doc.lines):
            return
        if order.changed_utc and local_date(order.changed_utc) >= self.today:
            res.held.append("%s cancelled today: closed tomorrow (H+1)" % order.name)
            return
        downstream = any(self.target.by_order(k, order.name) for k in ("receipt", "bill", "down_payment"))
        if downstream or any(l.processed_qty for l in doc.lines):
            res.manual.append("%s was cancelled but already has documents downstream" % order.name)
            return
        self.target.close(doc, "CANCELLED in source -- ")   # closed, never deleted; number kept
        res.created.append(("closed", doc.number))


def revise_order(target: Target, doc: TargetDoc, order: SourceOrder) -> list:
    """Bring a posted order in line with the source - all or nothing.

    A partial revision is worse than none: if one line can no longer change (it was already
    received), applying the others leaves a document that matches neither version. So every
    change is planned first, and nothing is written if any of them needs a person.
    """
    plan, manual, used = [], [], set()
    for want in order.lines:
        gp = gross_price(want)
        idx = next((i for i, l in enumerate(doc.lines) if l.product == want.product and i not in used), None)
        if idx is None:
            plan.append(("add", want))
            continue
        used.add(idx)
        have = doc.lines[idx]
        same_price = abs(have.gross_price - gp) * max(want.qty, 1) <= Decimal("0.5") and have.discount_pct == want.discount_pct
        if same_price and have.qty == want.qty:
            continue
        if have.processed_qty and not (same_price and want.qty >= have.processed_qty):
            manual.append("%s: %s changed after it was received" % (order.name, want.product))
            continue
        plan.append(("update", idx, want, gp))
    for i, have in enumerate(doc.lines):
        if i not in used:
            if have.processed_qty:
                manual.append("%s: %s removed after it was received" % (order.name, have.product))
            else:
                plan.append(("remove", i))
    if manual:
        return manual
    for step in plan:
        if step[0] == "update":
            _, idx, want, gp = step
            if doc.lines[idx].processed_qty:
                target.update_line(doc, idx, qty=want.qty)
            else:
                target.update_line(doc, idx, qty=want.qty, gross_price=gp, discount_pct=want.discount_pct)
    for step in sorted((s for s in plan if s[0] == "remove"), key=lambda s: -s[1]):
        target.remove_line(doc, step[1])
    for step in plan:
        if step[0] == "add":
            w = step[1]
            doc.lines.append(TargetLine(w.product, w.qty, gross_price(w), w.discount_pct, w.taxed))
    return []


# ---------------------------------------------------------------- verifier
def verify(source: Source, target: Target, name: str, user: str = "sync") -> list:
    """Compare one synced order chain with the source. Returns findings; empty means it matches."""
    order = source.order(name)
    docs = [d for d in target.by_order("order", name) if d.created_by == user]
    if order.state == "cancel":
        return [] if all(all(l.closed for l in d.lines) for d in docs) else ["%s cancelled in source but open in target" % name]
    if not docs:
        return ["%s missing in target" % name]
    doc, findings = docs[0], []
    if abs(doc.total - order.total) > 1:
        findings.append("%s total %s vs source %s" % (name, doc.total, order.total))
    want = sorted((l.product, l.qty) for l in order.lines)
    have = sorted((l.product, l.qty) for l in doc.lines)
    if want != have:
        findings.append("%s lines differ: %s vs %s" % (name, have, want))
    for inv in source.invoices(name):
        kind = "bill" if inv.kind == "final" else "down_payment"
        match = [d for d in target.by_order(kind, name) if d.number.rstrip(".") == inv.name]
        if match and abs(match[0].total - inv.amount_total) > 1:
            findings.append("%s amount %s vs source %s" % (inv.name, match[0].total, inv.amount_total))
    return findings
