"""Shares entered by amount, and the full-workbook import (everything in one file, placed in the right places)."""
import io
import re
from datetime import date

from openpyxl import load_workbook

from app.extensions import db
from app.models import (Contribution, Expense, GroupMember, IncomeRecord, Loan, LoanRepayment, ShareEntry, Transaction)
from app.services import ledger, shares as share_svc
from app.services import workbook as wb_svc


# ------------------------------------------------------------------ shares by amount
def _buy(world, **data):
    base = {"mode": "amount", "member_id": world.ma.id, "entry_date": date.today().isoformat(), "payment_method": "Cash"}
    base.update(data)
    return world.ca.post(f"/g/{world.ga.id}/shares/new", data=base)


def test_amount_becomes_whole_shares(app, world):
    price = share_svc.get_settings(world.ga.id).share_price
    assert share_svc.units_for_amount(world.ga.id, price * 5 + 123) == (5, price, 123)
    r = _buy(world, amount=str(price * 5 + 123))
    assert r.status_code == 302
    e = ShareEntry.query.filter_by(group_id=world.ga.id, member_id=world.ma.id).one()
    assert e.units == 5 and e.amount == 5 * price                       # only whole shares are recorded
    assert share_svc.member_units(world.ga.id, world.ma.id) == 5


def test_amount_below_one_share_is_refused_and_units_mode_still_works(app, world):
    price = share_svc.get_settings(world.ga.id).share_price
    _buy(world, amount=str(price - 1))
    assert ShareEntry.query.filter_by(group_id=world.ga.id).count() == 0
    _buy(world, mode="units", units="2")
    assert share_svc.member_units(world.ga.id, world.ma.id) == 2


def test_shares_form_has_live_calculator(app, world):
    page = world.ca.get(f"/g/{world.ga.id}/shares/new")
    assert page.status_code == 200 and b"data-share-form" in page.data and b"By amount paid" in page.data


# ------------------------------------------------------------------ full workbook
def _book(world, **extra):
    tpl = load_workbook(io.BytesIO(wb_svc.template_workbook()))

    def put(sheet, rows):
        for r in rows:
            tpl[sheet].append(r)
    put("Members", [["Grace Nambi", "0772111111", "", "MEMBER", "15/03/2023"],
                    ["John Okello", "0772222222", "", "TREASURER", ""],
                    ["Mark Member", "0772000003", "", "MEMBER", ""]])               # Mark is already in the group
    put("Contributions", [["0772111111", "", 50000, "05/06/2024", "MONTHLY", "Cash", "R-1", ""],
                          ["", "John Okello", 80000, "05/06/2024", "OPENING", "", "", ""]])
    put("Shares", [["0772111111", "", "", 55000, "01/07/2024", "", "Cash", ""],
                   ["0772222222", "", 3, "", "01/07/2024", "", "", ""]])
    put("Loans", [["Grace Nambi", "0772111111", 600000, 10, "MONTH", "FLAT", 6, "MONTH", "MONTHLY", "01/02/2025", "",
                   "L-101", "Stock"]])
    put("Repayments", [["L-101", "", 200000, "01/03/2025", "Mobile Money", "MM-1", ""]])
    put("Income", [["Registration fees", 20000, "10/01/2025", "New members", "Cash"]])
    put("Expenses", [["Annual meeting", "", 15000, "20/02/2025", "Cash", ""]])
    for sheet, rows in extra.items():
        put(sheet, rows)
    buf = io.BytesIO()
    tpl.save(buf)
    return buf.getvalue()


def _upload(client, gid, raw, name="everything.xlsx"):
    return client.post(f"/g/{gid}/import/workbook", data={"file": (io.BytesIO(raw), name)},
                       content_type="multipart/form-data")


def test_template_downloads_and_opens(app, world):
    r = world.ca.get(f"/g/{world.ga.id}/import/workbook-template.xlsx")
    assert r.status_code == 200
    names = load_workbook(io.BytesIO(r.data)).sheetnames
    assert names == ["Instructions", "Members", "Contributions", "Shares", "Loans", "Repayments", "Income", "Expenses"]
    assert world.cm.get(f"/g/{world.ga.id}/import/workbook-template.xlsx").status_code == 403


def test_workbook_preview_saves_nothing_then_confirm_places_everything(app, world):
    gid = world.ga.id
    r = _upload(world.ca, gid, _book(world))
    token = re.search(r"/import/full/(\w+)", r.headers["Location"]).group(1)
    page = world.ca.get(f"/g/{gid}/import/full/{token}")
    assert page.status_code == 200 and b"Preview only" in page.data
    assert GroupMember.query.filter_by(group_id=gid).count() == 2          # the preview saved nothing
    assert Loan.query.filter_by(group_id=gid).count() == 0

    assert world.ca.post(f"/g/{gid}/import/full/{token}/apply").status_code == 302
    assert GroupMember.query.filter_by(group_id=gid, status="ACTIVE").count() == 4      # Mark was kept, not duplicated
    grace = next(m for m in GroupMember.query.filter_by(group_id=gid) if m.user.phone == "0772111111")
    assert Contribution.query.filter_by(group_id=gid).count() == 2
    assert share_svc.member_units(gid, grace.id) == 5                                   # 55,000 at 10,000 -> 5 whole shares
    loan = Loan.query.filter_by(group_id=gid).one()
    assert loan.principal == 600000 and loan.disbursement_tx_id and len(loan.schedule) == 6
    rep = LoanRepayment.query.filter_by(group_id=gid, loan_id=loan.id).one()
    assert rep.amount == 200000 and rep.principal_part + rep.interest_part == 200000    # interest placed on the loan
    assert IncomeRecord.query.filter_by(group_id=gid).count() == 1
    assert Expense.query.filter_by(group_id=gid).count() == 1
    assert world.ca.post(f"/g/{gid}/import/full/{token}/apply").status_code == 302     # cannot apply twice
    assert Loan.query.filter_by(group_id=gid).count() == 1


def test_one_undo_reverses_the_whole_workbook(app, world):
    gid = world.ga.id
    token = re.search(r"/import/full/(\w+)", _upload(world.ca, gid, _book(world)).headers["Location"]).group(1)
    world.ca.post(f"/g/{gid}/import/full/{token}/apply")
    world.ca.post(f"/g/{gid}/import/full/{token}/undo")
    assert LoanRepayment.query.filter_by(group_id=gid).one().status == "REVERSED"
    assert Loan.query.filter_by(group_id=gid).one().status == "REJECTED"
    assert all(e.reversed_at for e in ShareEntry.query.filter_by(group_id=gid))
    assert all(ledger.is_reversed(c.tx_id) for c in Contribution.query.filter_by(group_id=gid))


def test_rows_can_refer_to_members_and_loans_in_the_same_workbook(app, world):
    raw = _book(world, Contributions=[["0770000000", "", 1000, "05/06/2024", "", "", "", ""]],
                Repayments=[["L-404", "", 1000, "01/03/2025", "", "", ""]])
    token = re.search(r"/import/full/(\w+)", _upload(world.ca, world.ga.id, raw).headers["Location"]).group(1)
    page = world.ca.get(f"/g/{world.ga.id}/import/full/{token}").data
    assert b"No active member with phone 0770000000" in world.ca.get(
        f"/g/{world.ga.id}/import/batch/{[b for b in wb_svc.get_bundle(world.ga.id, token) if b.kind == 'contributions'][0].id}").data
    assert b"Loan &#39;L-404&#39; was not found" in world.ca.get(
        f"/g/{world.ga.id}/import/batch/{[b for b in wb_svc.get_bundle(world.ga.id, token) if b.kind == 'repayments'][0].id}").data
    assert b"Confirm and import everything" in page                                      # good rows can still go in


def test_bad_files_and_access(app, world):
    gid = world.ga.id
    assert _upload(world.ca, gid, b"a,b\n1,2\n", "x.csv").headers["Location"].endswith("/import/")
    assert _upload(world.ca, gid, b"not an excel file", "x.xlsx").headers["Location"].endswith("/import/")
    token = re.search(r"/import/full/(\w+)", _upload(world.ca, gid, _book(world)).headers["Location"]).group(1)
    assert world.cm.get(f"/g/{gid}/import/full/{token}").status_code == 403           # members cannot import
    assert world.cb.get(f"/g/{gid}/import/full/{token}").status_code == 404           # another group cannot see it
