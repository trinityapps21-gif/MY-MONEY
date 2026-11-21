"""PDF receipts for every money/share movement, shared by download or WhatsApp."""
import io
import time
from datetime import date, timedelta
from unittest import mock

import pytest

from app.extensions import db
from app.models import (Borrower, Contribution, GroupMember, LoanFine, LoanRepayment, SavingsWithdrawal, ShareEntry,
                        ShareTransfer, Transaction)
from app.services import fines as fine_svc, receipts as rc, savings as savings_svc, shares as share_svc
from app.services.groupcfg import get_settings
from tests.conftest import PW, login, make_user
from tests.test_core import _make_loan


def _b(world):
    return f"/g/{world.ga.id}"


def _contribute(world, member, amount=50_000, key="c1"):
    r = world.ca.post(_b(world) + "/savings/new", data={"member_id": member.id, "amount": str(amount), "category": "MONTHLY",
                                                        "contribution_date": date.today().isoformat(), "idempotency_key": key})
    assert r.status_code == 302
    return Contribution.query.filter_by(group_id=world.ga.id).order_by(Contribution.id.desc()).first()


def _buy_shares(world, member, units=3):
    get_settings(world.ga.id).share_price = 10_000
    db.session.commit()
    r = world.ca.post(_b(world) + "/shares/new", data={"member_id": member.id, "units": str(units),
                                                       "entry_date": date.today().isoformat(), "payment_method": "Cash"})
    assert r.status_code == 302
    return ShareEntry.query.filter_by(group_id=world.ga.id, kind="PURCHASE").order_by(ShareEntry.id.desc()).first()


def _transfer(world, units=2):
    _buy_shares(world, world.ma, 5)
    t = share_svc.request_transfer(world.ga, world.mem_a, world.ma, world.adm_a_m, units, 10_000, "gift")
    db.session.commit()
    assert world.ca.post(f"{_b(world)}/shares/transfers/{t.id}/decide", data={"decision": "approve"}).status_code == 302
    return db.session.get(ShareTransfer, t.id)


def _withdrawal(world, amount=20_000):
    _contribute(world, world.ma, 100_000, "wd-seed")
    w = savings_svc.request_withdrawal(world.ga, world.mem_a, world.ma, amount)
    db.session.commit()
    r = world.ca.post(f"{_b(world)}/savings/withdrawals/{w.id}/decide", data={"decision": "approve", "payment_method": "Cash"})
    assert r.status_code == 302
    return db.session.get(SavingsWithdrawal, w.id)


def _text(resp):
    pypdf = pytest.importorskip("pypdf")
    return " ".join(" ".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(resp.data)).pages).split())


def _is_pdf(r):
    return r.status_code == 200 and r.mimetype == "application/pdf" and r.data[:4] == b"%PDF"


# ---------------------------------------------------------------- one receipt per kind of activity
def test_contribution_receipt(app, world):
    c = _contribute(world, world.ma)
    r = world.ca.get(f"{_b(world)}/receipts/contribution/{c.id}")
    assert _is_pdf(r)
    t = _text(r)
    assert "CONTRIBUTION RECEIPT" in t and c.receipt_no in t and "Mark Member" in t and "50,000" in t


def test_share_purchase_receipt(app, world):
    e = _buy_shares(world, world.ma, 3)
    r = world.ca.get(f"{_b(world)}/receipts/share/{e.id}")
    assert _is_pdf(r)
    t = _text(r)
    assert "SHARE PURCHASE RECEIPT" in t and e.receipt_no in t and "Shares bought 3" in t and "30,000" in t and "Shares held now 3" in t


def test_share_transfer_receipt(app, world):
    tr = _transfer(world, 2)
    r = world.ca.get(f"{_b(world)}/receipts/share_transfer/{tr.id}")
    assert _is_pdf(r)
    t = _text(r)
    assert "SHARE TRANSFER RECEIPT" in t and f"ST-{tr.id:06d}" in t and "Mark Member" in t and "Ann Admin" in t
    assert "Shares transferred 2" in t and "20,000" in t and "Approved by Ann Admin" in t


def test_loan_instalment_payment_receipt(app, world):
    loan, b = _make_loan(world)
    world.ca.post(b + "/repayments/new", data={"loan_id": loan.id, "amount": "400000", "payment_date": date.today().isoformat(),
                                               "payment_method": "Cash", "idempotency_key": "r1"})
    rep = LoanRepayment.query.filter_by(loan_id=loan.id).one()
    r = world.ca.get(f"{b}/receipts/repayment/{rep.id}")
    assert _is_pdf(r)
    t = _text(r)
    assert "LOAN REPAYMENT RECEIPT" in t and rep.receipt_no in t and loan.reference in t and "Carl Trader" in t and "400,000" in t
    d = world.ca.get(f"{b}/receipts/disbursement/{loan.id}")
    assert _is_pdf(d) and "LOAN DISBURSEMENT RECEIPT" in _text(d)


def test_withdrawal_receipt(app, world):
    w = _withdrawal(world)
    r = world.ca.get(f"{_b(world)}/receipts/withdrawal/{w.id}")
    assert _is_pdf(r)
    t = _text(r)
    assert "SAVINGS WITHDRAWAL RECEIPT" in t and w.receipt_no in t and "20,000" in t and "Paid to Mark Member" in t


def test_late_fine_receipt_and_any_ledger_transaction(app, world):
    loan, b = _make_loan(world)
    f = LoanFine(group_id=world.ga.id, loan_id=loan.id, borrower_id=loan.borrower_id, deadline=date.today() - timedelta(days=20),
                 paid_on=date.today(), days_late=13, late_amount=100_000, amount=5_000)
    db.session.add(f)
    db.session.flush()
    fine_svc.settle(world.ga, f, world.admin_a, date.today(), "Cash")
    db.session.commit()
    r = world.ca.get(f"{b}/receipts/fine/{f.id}")
    assert _is_pdf(r) and "LATE FINE RECEIPT" in _text(r) and "5,000" in _text(r)
    tx = Transaction.query.filter_by(group_id=world.ga.id).order_by(Transaction.id.desc()).first()
    assert _is_pdf(world.ca.get(f"{b}/receipts/transaction/{tx.id}"))


def test_reversed_activity_is_marked_on_the_receipt(app, world):
    c = _contribute(world, world.ma)
    world.ca.post(f"{_b(world)}/savings/{c.id}/reverse", data={"reason": "mistake"})
    assert "REVERSED" in _text(world.ca.get(f"{_b(world)}/receipts/contribution/{c.id}"))


# ---------------------------------------------------------------- who may open what
def test_member_sees_only_their_own_and_never_borrower_receipts(app, world):
    mine, other = _contribute(world, world.ma, 50_000, "k1"), _contribute(world, world.adm_a_m, 70_000, "k2")
    base = _b(world) + "/receipts"
    assert _is_pdf(world.cm.get(f"{base}/contribution/{mine.id}"))
    assert world.cm.get(f"{base}/contribution/{other.id}").status_code == 404
    e = _buy_shares(world, world.ma, 2)
    assert _is_pdf(world.cm.get(f"{base}/share/{e.id}"))
    e2 = _buy_shares(world, world.adm_a_m, 2)
    assert world.cm.get(f"{base}/share/{e2.id}").status_code == 404
    w = _withdrawal(world)
    assert _is_pdf(world.cm.get(f"{base}/withdrawal/{w.id}"))
    loan, b = _make_loan(world)
    for kind, oid in (("disbursement", loan.id), ("transaction", Transaction.query.first().id)):
        assert world.cm.get(f"{b}/receipts/{kind}/{oid}").status_code == 404


def test_transfer_receipt_visible_to_both_parties_only(app, world):
    tr = _transfer(world, 2)
    base = _b(world) + "/receipts/share_transfer"
    assert _is_pdf(world.cm.get(f"{base}/{tr.id}"))                       # the seller
    third = make_user("Tess Third", "0772000077", "t@x.com")
    from tests.conftest import add_member
    add_member(world.ga, third)
    assert login(app, "t@x.com").get(f"{base}/{tr.id}").status_code == 404


def test_unavailable_receipts_and_outsiders(app, world):
    base = _b(world) + "/receipts"
    _buy_shares(world, world.ma, 5)
    t = share_svc.request_transfer(world.ga, world.mem_a, world.ma, world.adm_a_m, 1, 10_000)
    w = savings_svc.request_withdrawal(world.ga, world.mem_a, world.ma, 1_000) if False else None
    db.session.commit()
    assert world.ca.get(f"{base}/share_transfer/{t.id}").status_code == 404            # still pending
    assert world.ca.get(f"{base}/nonsense/1").status_code == 404
    assert world.ca.get(f"{base}/contribution/999999").status_code == 404
    c = _contribute(world, world.ma)
    assert world.cb.get(f"{base}/contribution/{c.id}").status_code in (403, 404)       # other group's admin
    assert app.test_client().get(f"{base}/contribution/{c.id}").status_code in (302, 401)


# ---------------------------------------------------------------- sharing on WhatsApp
def test_share_endpoint_gives_link_text_and_whatsapp_urls(app, world):
    c = _contribute(world, world.ma)
    r = world.ca.post(f"{_b(world)}/receipts/contribution/{c.id}/share", headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["url"].startswith("http") and "/r/" in d["url"] and d["has_phone"] is True and d["days"] == 30
    assert c.receipt_no in d["text"] and "50,000" in d["text"] and "Group A" in d["text"] and d["url"] in d["text"]
    assert d["whatsapp"].startswith("https://wa.me/256772000003?text=")        # the member's own number, 0772... -> 256772...
    assert d["whatsapp_choose"].startswith("https://wa.me/?text=")
    assert world.cm.post(f"{_b(world)}/receipts/contribution/{c.id}/share").status_code == 200          # their own
    other = _contribute(world, world.adm_a_m, 1_000, "z")
    assert world.cm.post(f"{_b(world)}/receipts/contribution/{other.id}/share").status_code == 404      # not theirs


def test_public_link_opens_pdf_without_login_and_is_safe(app, world):
    c = _contribute(world, world.ma)
    url = world.ca.post(f"{_b(world)}/receipts/contribution/{c.id}/share").get_json()["url"]
    path = url.split("://", 1)[1].split("/", 1)[1]
    anon = app.test_client()
    r = anon.get("/" + path)
    assert _is_pdf(r) and c.receipt_no in _text(r)
    assert "noindex" in r.headers["X-Robots-Tag"] and "no-store" in r.headers["Cache-Control"]
    assert anon.get("/" + path[:-3] + "xyz").status_code == 404                           # tampered
    assert anon.get("/r/not-a-token").status_code == 404
    with mock.patch("itsdangerous.timed.time.time", return_value=time.time() + 31 * 86400):
        assert anon.get("/" + path).status_code == 404                                    # expired after 30 days
    world.ca.post(f"{_b(world)}/savings/{c.id}/reverse", data={"reason": "oops"})
    assert "REVERSED" in _text(anon.get("/" + path))                                      # link shows the current state


def test_public_link_cannot_be_pointed_at_another_group(app, world):
    c = _contribute(world, world.ma)
    forged = rc.make_token(world.gb.id, "contribution", c.id)                              # right id, wrong group
    assert app.test_client().get(f"/r/{forged}").status_code == 404


@pytest.mark.parametrize("raw,expected", [("0772123456", "256772123456"), ("+256 772 123 456", "256772123456"),
                                          ("00256772123456", "256772123456"), ("256772123456", "256772123456"),
                                          ("", None), (None, None), ("12", None), ("abc", None)])
def test_whatsapp_number_normalising(app, raw, expected):
    with app.app_context():
        assert rc.wa_number(raw) == expected


# ---------------------------------------------------------------- "receipt ready" prompt and the buttons
def test_receipt_ready_prompt_appears_once_after_recording(app, world):
    world.ca.post(_b(world) + "/savings/new", data={"member_id": world.ma.id, "amount": "50000", "category": "MONTHLY",
                                                    "contribution_date": date.today().isoformat(), "idempotency_key": "p1"})
    html = world.ca.get(_b(world) + "/savings/").data.decode()
    assert "Receipt ready" in html and "data-rcpt-share" in html and "Contribution Receipt" in html
    assert "Receipt ready" not in world.ca.get(_b(world) + "/savings/").data.decode()


def test_prompt_after_share_purchase_transfer_withdrawal_and_repayment(app, world):
    _buy_shares(world, world.ma, 3)
    assert "Share Purchase Receipt" in world.ca.get(_b(world) + "/shares/").data.decode()
    _transfer(world, 1)
    assert "Share Transfer Receipt" in world.ca.get(_b(world) + "/shares/transfers").data.decode()
    _withdrawal(world)
    assert "Savings Withdrawal Receipt" in world.ca.get(_b(world) + "/savings/withdrawals").data.decode()
    loan, b = _make_loan(world)
    world.ca.post(b + "/repayments/new", data={"loan_id": loan.id, "amount": "100000", "payment_date": date.today().isoformat(),
                                               "payment_method": "Cash", "idempotency_key": "rr"})
    assert "Loan Repayment Receipt" in world.ca.get(f"{b}/loans/{loan.id}").data.decode()


@pytest.mark.parametrize("path,client", [("/savings/", "ca"), ("/savings/me", "cm"), ("/shares/", "ca"), ("/shares/me", "cm"),
                                         ("/shares/transfers", "ca"), ("/savings/withdrawals", "ca"), ("/repayments/", "ca"),
                                         ("/transactions/", "ca"), ("/fines/", "ca")])
def test_receipt_buttons_are_on_every_list(app, world, path, client):
    c = _contribute(world, world.ma)
    _buy_shares(world, world.ma, 3)
    _transfer(world, 1)
    _withdrawal(world)
    loan, b = _make_loan(world)
    world.ca.post(b + "/repayments/new", data={"loan_id": loan.id, "amount": "100000", "payment_date": date.today().isoformat(),
                                               "payment_method": "Cash", "idempotency_key": "rb"})
    html = getattr(world, client).get(_b(world) + path).data.decode()
    if path != "/fines/":
        assert "data-rcpt-share" in html and "Download PDF receipt" in html, path
    assert "js/receipts.js" in html


def test_share_sheet_script_is_served(app):
    assert app.test_client().get("/static/js/receipts.js").status_code in (200, 302)
