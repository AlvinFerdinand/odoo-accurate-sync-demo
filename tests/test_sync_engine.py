# -*- coding: utf-8 -*-
"""Every rule here exists because a real document broke without it. Data is invented."""
import copy
import sys
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sync_engine import (ApiRejected, Line, Source, SourceInvoice, SourceOrder, SourcePicking, SyncEngine,
                         Target, free_number, local_date, verify)

TODAY = date(2026, 10, 22)
YESTERDAY = "2026-10-21T03:00:00"


def po(name="PO/0001", lines=None, partner="Vendor A", when=YESTERDAY, **kw):
    lines = lines or (Line("CAM-1", D(2), D("1000000")), Line("CABLE", D(100), D("4000"), D(5)))
    return SourceOrder(name, partner, when, tuple(lines), **kw)


def receipt(name="IN/0001", order="PO/0001", moves=(("CAM-1", 2),), when=YESTERDAY, state="done"):
    return SourcePicking(name, order, when, tuple(moves), state)


def run(source, target=None, today=TODAY, name="PO/0001"):
    target = target or Target()
    return SyncEngine(source, target, today).sync_purchase(name), target


# ---------------------------------------------------------------- dates & H+1
def test_utc_evening_belongs_to_next_local_day():
    assert local_date("2026-10-20T18:30:00") == date(2026, 10, 21)
    assert local_date("2026-10-20T16:59:00") == date(2026, 10, 20)


def test_order_dated_today_waits_until_tomorrow():
    res, target = run(Source([po(when="2026-10-22T02:00:00")]))
    assert not target.docs and "H+1" in res.held[0]


def test_receipt_done_today_is_held_and_so_is_its_bill():
    src = Source([po()], [receipt(when="2026-10-22T03:00:00")],
                 [SourceInvoice("BILL/0001", "PO/0001", "2026-10-22", D("2642800"))])
    res, target = run(src)
    assert target.find("receipt", "IN/0001") is None
    assert target.find("bill", "BILL/0001") is None
    assert any("waits for its goods receipt" in h for h in res.held)


# ---------------------------------------------------------------- creating documents
def test_vat_inclusive_prices_keep_totals_equal_to_source():
    order = po()
    res, target = run(Source([order]))
    doc = target.find("order", "PO/0001")
    assert doc.total == order.total                      # line discount included
    assert ("order", "PO/0001") in res.created


def test_untaxed_line_stays_untaxed():
    order = po(lines=(Line("SERVICE", D(1), D("500000"), taxed=False),))
    _, target = run(Source([order]))
    assert target.find("order", "PO/0001").total == D("500000.00")


def test_only_done_receipts_with_quantity_are_copied():
    src = Source([po()], [receipt("IN/0001"), receipt("IN/0002", moves=(("CABLE", 0),)),
                          receipt("IN/0003", state="assigned")])
    _, target = run(src)
    assert target.find("receipt", "IN/0001")
    assert target.find("receipt", "IN/0002") is None     # done with qty 0: nothing arrived
    assert target.find("receipt", "IN/0003") is None     # not done yet


def test_second_run_creates_nothing_new():
    src = Source([po()], [receipt()], [SourceInvoice("BILL/0001", "PO/0001", "2026-10-21", D("2642800"))])
    _, target = run(src)
    count = len(target.docs)
    res, _ = run(src, target)
    assert len(target.docs) == count and res.created == []


def test_no_down_payment_once_the_final_bill_exists():
    src = Source([po()], [receipt()], [SourceInvoice("BILL/0001", "PO/0001", "2026-10-21", D("2642800"))])
    _, target = run(src)
    late_dp = Source([po()], [receipt()], [SourceInvoice("BILL/0001", "PO/0001", "2026-10-21", D("2642800")),
                                           SourceInvoice("DP/0001", "PO/0001", "2026-10-21", D("500000"), "down_payment")])
    run(late_dp, target)
    assert target.find("down_payment", "DP/0001") is None


# ---------------------------------------------------------------- numbers reused by another company
def test_free_number_recognises_the_same_document():
    target = Target()
    target.create(kind="bill", number="BILL/0034", partner="Vendor A", doc_date=TODAY, created_by="sync", amount=D(100))
    assert free_number(target, "bill", "BILL/0034", "Vendor A", D(100)) is None


def test_number_used_by_an_unrelated_document_gets_a_dot():
    target = Target()
    target.create(kind="order", number="PO/0001", partner="Old Company Vendor", doc_date=TODAY,
                  created_by="someone", amount=D("13320000"))
    res, target = run(Source([po()]), target)
    assert ("order", "PO/0001.") in res.created
    assert verify(Source([po()]), target, "PO/0001") == []


# ---------------------------------------------------------------- revisions after posting
def test_price_change_on_unreceived_line_is_applied():
    _, target = run(Source([po()]))
    revised = po(lines=(Line("CAM-1", D(2), D("950000")), Line("CABLE", D(100), D("4000"), D(5))))
    res, _ = run(Source([revised]), target)
    assert res.manual == [] and verify(Source([revised]), target, "PO/0001") == []


def test_quantity_increase_on_received_line_is_allowed():
    _, target = run(Source([po()], [receipt()]))
    more = po(lines=(Line("CAM-1", D(3), D("1000000")), Line("CABLE", D(100), D("4000"), D(5))))
    res, _ = run(Source([more], [receipt()]), target)
    assert res.manual == [] and target.find("order", "PO/0001").lines[0].qty == 3


def test_revision_is_all_or_nothing():
    _, target = run(Source([po()], [receipt()]))
    before = copy.deepcopy(target.find("order", "PO/0001").lines)
    # CAM-1 was received, so its price may not change; the CABLE change must not be applied alone
    revised = po(lines=(Line("CAM-1", D(2), D("900000")), Line("CABLE", D(120), D("4000"), D(5))))
    res, _ = run(Source([revised], [receipt()]), target)
    assert res.manual and "changed after it was received" in res.manual[0]
    assert target.find("order", "PO/0001").lines == before


def test_removed_lines_follow_the_same_rule():
    _, target = run(Source([po()], [receipt()]))
    without_cable = po(lines=(Line("CAM-1", D(2), D("1000000")),))
    res, _ = run(Source([without_cable], [receipt()]), target)
    assert res.manual == [] and [l.product for l in target.find("order", "PO/0001").lines] == ["CAM-1"]
    without_cam = po(lines=(Line("CABLE", D(100), D("4000"), D(5)),))
    res, _ = run(Source([without_cam], [receipt()]), target)
    assert "removed after it was received" in res.manual[0]


def test_target_api_refuses_what_the_real_one_refuses():
    _, target = run(Source([po()], [receipt()]))
    doc = target.find("order", "PO/0001")
    with pytest.raises(ApiRejected):
        target.update_line(doc, 0, gross_price=D(1))
    with pytest.raises(ApiRejected):
        target.update_line(doc, 0, qty=D(1))              # below the 2 already received


# ---------------------------------------------------------------- cancellations
def test_cancelled_order_is_closed_not_deleted():
    _, target = run(Source([po()]))
    cancelled = po(state="cancel", changed_utc=YESTERDAY)
    res, _ = run(Source([cancelled]), target)
    doc = target.find("order", "PO/0001")
    assert doc is not None and all(l.closed for l in doc.lines)
    assert doc.description.startswith("CANCELLED") and ("closed", "PO/0001") in res.created
    assert verify(Source([cancelled]), target, "PO/0001") == []


def test_cancellation_made_today_waits_a_day():
    _, target = run(Source([po()]))
    res, _ = run(Source([po(state="cancel", changed_utc="2026-10-22T05:14:00")]), target)
    assert not any(l.closed for l in target.find("order", "PO/0001").lines) and "H+1" in res.held[0]


def test_cancelled_order_with_a_receipt_needs_a_person():
    _, target = run(Source([po()], [receipt()]))
    res, _ = run(Source([po(state="cancel", changed_utc=YESTERDAY)], [receipt()]), target)
    assert "downstream" in res.manual[0]
    assert not any(l.closed for l in target.find("order", "PO/0001").lines)


# ---------------------------------------------------------------- what the engine must never do
def test_documents_entered_by_people_are_never_touched():
    target = Target()
    target.create(kind="order", number="PO/0001", partner="Vendor A", doc_date=TODAY, created_by="finance-staff",
                  lines=SyncEngine(Source(), target, TODAY)._target_lines(po()), order="PO/0001")
    before = copy.deepcopy(target.find("order", "PO/0001"))
    run(Source([po(lines=(Line("CAM-1", D(9), D("1")),))]), target)
    run(Source([po(state="cancel", changed_utc=YESTERDAY)]), target)
    assert target.find("order", "PO/0001") == before


def test_engine_never_deletes_and_never_writes_the_source():
    class NoDelete(Target):
        def delete(self, doc):
            raise AssertionError("the sync must never delete")

    src = Source([po()], [receipt()], [SourceInvoice("BILL/0001", "PO/0001", "2026-10-21", D("2642800"))])
    snapshot = copy.deepcopy(src.__dict__)
    target = NoDelete()
    run(src, target)
    run(Source([po(state="cancel", changed_utc=YESTERDAY)]), target)
    assert src.__dict__ == snapshot


# ---------------------------------------------------------------- verifier
def test_verifier_catches_a_wrong_total():
    src = Source([po()])
    _, target = run(src)
    assert verify(src, target, "PO/0001") == []
    target.find("order", "PO/0001").lines[0].gross_price += 1000
    assert "total" in verify(src, target, "PO/0001")[0]


def test_verifier_reports_a_missing_order():
    assert verify(Source([po()]), Target(), "PO/0001") == ["PO/0001 missing in target"]
