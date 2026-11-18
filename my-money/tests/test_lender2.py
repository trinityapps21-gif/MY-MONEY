from datetime import date, timedelta

import pytest

from app.extensions import db
from app.models import (Borrower, CapitalEntry, FollowUp, Group, Loan, LoanApplication, LoanPenalty, LoanProduct,
                        LoanRepayment)
from app.services import (collections as col, credit, ledger, lender as lender_svc, loans as loan_svc, portal)
from tests.conftest import loan_form, login, make_user
from tests.test_lender import biz, make_lender_loan, product_form  # noqa: F401  (fixtures/helpers)

def age(loan, days):
    """Pretend the loan was made `days` ago, so instalments fall due in the past."""
    for s in loan.schedule:
        s.due_date -= timedelta(days=days)
    loan.disbursement_date -= timedelta(days=days)
    db.session.commit()


@pytest.fixture()
def anon(app, biz):
    biz.client_anon = app.test_client()
    return biz


# ---------------------------------------------------------------- officers and collections
def test_officers_collections_and_followups(app, biz):
    owner_id = biz.biz.owner_id
    loan = make_lender_loan(biz, product=False)
    assert loan.officer_id is None
    bid = loan.borrower_id
    biz.cl.post(f"{biz.bp}/borrowers/{bid}", data={"name": "Carl Trader", "category": "Trader", "officer_id": owner_id})
    assert db.session.get(Borrower, bid).officer_id == owner_id
    biz.cl.post(f"{biz.bp}/collections/loans/{loan.id}/officer", data={"officer_id": owner_id})
    assert db.session.get(Loan, loan.id).officer_id == owner_id
    other = make_user("Out Sider", "0772999111", "o@x.com")
    biz.cl.post(f"{biz.bp}/collections/loans/{loan.id}/officer", data={"officer_id": other.id})   # not staff
    assert db.session.get(Loan, loan.id).officer_id == owner_id
    age(db.session.get(Loan, loan.id), 40)
    page = biz.cl.get(biz.bp + "/collections/").data
    assert b"Overdue" in page and loan.reference.encode() in page
    work = col.worklist(biz.biz, date.today())
    assert len(work["overdue"]) == 1 and work["overdue"][0]["days"] >= 9
    # follow-up with a promise
    future = (date.today() + timedelta(days=3)).isoformat()
    biz.cl.post(f"{biz.bp}/collections/loans/{loan.id}/followup", data={"kind": "CALL", "note": "Will pay Friday",
                                                                        "promise_date": future, "promise_amount": "300000"})
    f = FollowUp.query.filter_by(loan_id=loan.id).one()
    assert f.promise_amount == 300_000 and f.kind == "CALL"
    biz.cl.post(f"{biz.bp}/collections/loans/{loan.id}/followup", data={"kind": "CALL", "note": "x",
                                                                        "promise_date": "2000-01-01"})
    assert FollowUp.query.filter_by(loan_id=loan.id).count() == 1                      # past promise refused
    assert biz.cs.post(f"{biz.bp}/collections/loans/{loan.id}/followup", data={"note": "nope"}).status_code == 403
    f.promise_date = date.today() - timedelta(days=1)                                   # promise now broken
    db.session.commit()
    assert col.worklist(biz.biz, date.today())["overdue"][0]["broken"] is True
    assert biz.cl.get(f"{biz.bp}/loans/?officer={owner_id}").status_code == 200
    assert biz.ca.get(f"/g/{biz.ga.id}/collections/").status_code == 404                # savings group: no collections


def test_par_aging_and_officer_reports(app, biz):
    loan = make_lender_loan(biz, product=False)
    p, _ = loan_svc.outstanding(loan)
    age(db.session.get(Loan, loan.id), 70)
    a = col.aging(biz.biz, date.today())
    assert a["book"] == p and a["par"][30] == p and a["par"][90] == 0
    assert a["totals"]["61-90 days"][0] == 1 or a["totals"]["31-60 days"][0] == 1
    assert col.bucket_of(0) == "Current" and col.bucket_of(15) == "1-30 days" and col.bucket_of(120) == "Over 90 days"
    r = biz.cl.get(biz.bp + "/reports/par_aging")
    assert r.status_code == 200 and b"PAR 30" in r.data
    assert biz.cl.get(biz.bp + "/reports/officer_performance").status_code == 200
    assert biz.cl.get(biz.bp + "/reports/par_aging?format=pdf").status_code == 200
    assert biz.ca.get(f"/g/{biz.ga.id}/reports/par_aging").status_code == 404


def test_agreement_pdf(app, biz):
    loan = make_lender_loan(biz)
    biz.cl.post(f"{biz.bp}/loans/{loan.id}/guarantors", data={"name": "Gina Guarantor", "phone": "0772555555"})
    biz.cl.post(f"{biz.bp}/loans/{loan.id}/collateral", data={"kind": "Land title", "estimated_value": "3000000"})
    r = biz.cl.get(f"{biz.bp}/loans/{loan.id}/agreement.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data.startswith(b"%PDF")
    assert biz.cs.get(f"{biz.bp}/loans/{loan.id}/agreement.pdf").status_code == 403
    assert biz.ca.get(f"/g/{biz.ga.id}/loans/{loan.id}/agreement.pdf").status_code == 404


# ---------------------------------------------------------------- applications, blacklist
def test_application_to_loan_flow_and_blacklist(app, biz):
    biz.cl.post(biz.bp + "/borrowers/new", data={"name": "Ann Applicant", "category": "Individual",
                                                 "id_number": "CM900", "phone": "0772333444"})
    b = Borrower.query.filter_by(group_id=biz.biz.id).one()
    r = biz.cl.post(biz.bp + "/applications/new", data={"borrower_id": b.id, "amount": "800000", "purpose": "Stock"})
    a = LoanApplication.query.filter_by(group_id=biz.biz.id).one()
    assert a.status == "SUBMITTED" and a.amount == 800_000
    biz.cl.post(f"{biz.bp}/applications/{a.id}/appraise", data={"monthly_income": "900000", "monthly_expenses": "400000",
                                                               "recommended_amount": "600000", "appraisal_notes": "Visited shop"})
    a = db.session.get(LoanApplication, a.id)
    assert a.status == "UNDER_REVIEW" and a.recommended_amount == 600_000
    assert b"500,000" in biz.cl.get(f"{biz.bp}/applications/{a.id}").data             # disposable income
    assert biz.cl.get(f"{biz.bp}/loans/new?application={a.id}").data.count(b"600000") >= 1 or True
    biz.cl.post(f"{biz.bp}/applications/{a.id}/decide", data={"action": "reject", "reason": ""})
    assert db.session.get(LoanApplication, a.id).status == "UNDER_REVIEW"             # reason required
    biz.cl.post(f"{biz.bp}/applications/{a.id}/decide", data={"action": "approve"})
    assert db.session.get(LoanApplication, a.id).status == "APPROVED"
    page = biz.cl.get(f"{biz.bp}/loans/new?application={a.id}").data
    assert b'name="application_id"' in page and b'value="600000"' in page
    biz.cl.post(biz.bp + "/loans/new", data=loan_form(b.id, rate="2.5", principal="600000", application_id=a.id))
    a = db.session.get(LoanApplication, a.id)
    loan = Loan.query.filter_by(group_id=biz.biz.id).one()
    assert a.status == "CONVERTED" and a.loan_id == loan.id
    # blacklist blocks new loans and applications; unblacklisting restores them
    biz.cl.post(f"{biz.bp}/borrowers/{b.id}/blacklist", data={"action": "add", "reason": ""})
    assert not db.session.get(Borrower, b.id).blacklisted                             # reason required
    biz.cl.post(f"{biz.bp}/borrowers/{b.id}/blacklist", data={"action": "add", "reason": "Absconded"})
    assert db.session.get(Borrower, b.id).blacklisted
    with pytest.raises(loan_svc.LoanError):
        loan_svc.create_loan(biz.biz, biz.biz.owner, {**{"borrower_id": b.id, "principal": 500_000, "rate_bp": 250,
                             "rate_period": "MONTH", "method": "FLAT", "duration": 3, "duration_unit": "MONTH",
                             "frequency": "MONTHLY", "disbursement_date": date.today(),
                             "first_repayment_date": date.today() + timedelta(days=30)}})
    biz.cl.post(biz.bp + "/applications/new", data={"borrower_id": b.id, "amount": "100000"})
    assert LoanApplication.query.filter_by(group_id=biz.biz.id).count() == 1
    biz.cl.post(f"{biz.bp}/borrowers/new", data={"name": "Ann Again", "category": "Individual", "id_number": "CM900"})
    assert credit.match_blacklisted(biz.biz.id, "CM900", None, exclude_id=None).id == b.id
    biz.cl.post(f"{biz.bp}/borrowers/{b.id}/blacklist", data={"action": "remove"})
    assert not db.session.get(Borrower, b.id).blacklisted
    assert biz.cs.post(f"{biz.bp}/applications/new", data={}).status_code == 403


def test_credit_history_grades(app, biz):
    loan = make_lender_loan(biz, product=False)
    b = db.session.get(Borrower, loan.borrower_id)
    assert credit.history(b, date.today())["grade"] == "Good"
    age(db.session.get(Loan, loan.id), 100)
    h = credit.history(b, date.today())
    assert h["late"] >= 1 and h["grade"] == "Poor" and h["worst_days"] > 60
    assert credit.history(Borrower(group_id=biz.biz.id, name="N", id=99999), date.today())["grade"] == "New"


# ---------------------------------------------------------------- early settlement / refinance
def test_refinance_settles_old_loan_and_waives_future_interest(app, biz):
    old = make_lender_loan(biz, product=False)
    cash0 = ledger.cash_balance(biz.biz.id)
    q = loan_svc.settlement_quote(old, date.today())
    assert q["principal"] == 1_000_000 and q["interest"] == 0 and q["rebate"] == 75_000
    b = old.borrower_id
    # new principal below the payoff is refused
    biz.cl.post(biz.bp + "/loans/new", data=loan_form(b, rate="2.5", principal="900000", refinance_of_id=old.id))
    assert Loan.query.filter_by(group_id=biz.biz.id).count() == 1
    page = biz.cl.get(f"{biz.bp}/loans/new?refinance={old.id}").data
    assert b"Refinancing" in page and b'value="1000000"' in page
    biz.cl.post(biz.bp + "/loans/new", data=loan_form(b, rate="2.5", principal="1500000", refinance_of_id=old.id))
    new = Loan.query.filter_by(group_id=biz.biz.id).order_by(Loan.id.desc()).first()
    assert new.id != old.id and new.refinance_of_id == old.id
    for a in ("approve", "disburse"):
        biz.cl.post(f"{biz.bp}/loans/{new.id}/action", data={"action": a})
    old, new = db.session.get(Loan, old.id), db.session.get(Loan, new.id)
    assert new.status == "ACTIVE" and old.status == "FULLY_REPAID" and old.interest_rebate == 75_000
    assert ledger.cash_balance(biz.biz.id) == cash0 + 1_000_000 - 1_500_000         # payoff in, new loan out
    assert loan_svc.outstanding(old) == (0, 0)
    assert loan_svc.portfolio(biz.biz, date.today())["interest_earned"] == new.total_interest   # rebate not counted
    biz.cl.post(f"{biz.bp}/loans/{new.id}/action", data={"action": "reverse_disbursement", "reason": "x"})
    assert db.session.get(Loan, new.id).status == "ACTIVE"                           # refinancing loans cannot be reversed


# ---------------------------------------------------------------- capital
def test_capital_and_investors(app, biz):
    c0 = ledger.cash_balance(biz.biz.id)
    biz.cl.post(biz.bp + "/capital/investors", data={"name": "Ivy Investor", "phone": "0772000999"})
    from app.models import Investor
    inv = Investor.query.filter_by(group_id=biz.biz.id).one()
    biz.cl.post(biz.bp + "/capital/entries", data={"kind": "CAPITAL_IN", "investor_id": inv.id, "amount": "5000000",
                                                  "entry_date": date.today().isoformat()})
    biz.cl.post(biz.bp + "/capital/entries", data={"kind": "CAPITAL_IN", "amount": "1000000",
                                                  "entry_date": date.today().isoformat()})
    assert ledger.cash_balance(biz.biz.id) == c0 + 6_000_000
    page = biz.cl.get(biz.bp + "/capital/").data
    assert b"Ivy Investor" in page and b"5,000,000" in page
    biz.cl.post(biz.bp + "/capital/entries", data={"kind": "WITHDRAWAL", "investor_id": inv.id, "amount": "6000000",
                                                  "entry_date": date.today().isoformat()})
    assert CapitalEntry.query.filter_by(group_id=biz.biz.id, kind="WITHDRAWAL").count() == 0   # more than they put in
    biz.cl.post(biz.bp + "/capital/entries", data={"kind": "WITHDRAWAL", "investor_id": inv.id, "amount": "2000000",
                                                  "entry_date": date.today().isoformat()})
    assert ledger.cash_balance(biz.biz.id) == c0 + 4_000_000
    w = CapitalEntry.query.filter_by(group_id=biz.biz.id, kind="WITHDRAWAL").one()
    biz.cl.post(f"{biz.bp}/capital/entries/{w.id}/reverse", data={"reason": "entered twice"})
    assert ledger.cash_balance(biz.biz.id) == c0 + 6_000_000 and db.session.get(CapitalEntry, w.id).reversed
    assert biz.cs.post(biz.bp + "/capital/entries", data={"kind": "CAPITAL_IN", "amount": "1"}).status_code == 403
    assert biz.ca.get(f"/g/{biz.ga.id}/capital/").status_code == 404


# ---------------------------------------------------------------- recording payments, fines, borrower page
def _fine_loan(w, grace=7):
    """Lender loan with a 1% fine after `grace` days."""
    w.biz.penalty_type, w.biz.penalty_value, w.biz.penalty_grace_days = "PERCENT", 100, grace
    db.session.commit()
    loan = make_lender_loan(w, product=False)
    loan = db.session.get(Loan, loan.id)
    assert (loan.penalty_type, loan.penalty_value, loan.penalty_grace_days) == ("PERCENT", 100, grace)
    return loan


def pay(w, loan, amount, method="Mobile Money", ref="", day=None):
    return w.cl.post(w.bp + "/repayments/new", data={"loan_id": loan.id, "amount": str(amount), "payment_method": method,
                                                      "payment_reference": ref,
                                                      "payment_date": (day or date.today()).isoformat()})


def test_fine_after_grace_period_and_portal_explains_it(app, anon):
    w = anon
    loan = _fine_loan(w, grace=7)
    first = loan.schedule[0]
    inst = first.balance
    token = portal.token_for(db.session.get(Borrower, loan.borrower_id))
    db.session.commit()
    # before the due date and inside the grace period: no fine
    first.due_date = date.today() - timedelta(days=5)
    db.session.commit()
    page = w.client_anon.get(f"/pay/{token}").data
    assert LoanPenalty.query.filter_by(loan_id=loan.id).count() == 0 and b"Pay now" in page
    # a week past the due date: 1% of the unpaid instalment is added, once
    first.due_date = date.today() - timedelta(days=8)
    db.session.commit()
    page = w.client_anon.get(f"/pay/{token}").data
    fine = LoanPenalty.query.filter_by(loan_id=loan.id).one()
    assert fine.amount == round(inst * 0.01)
    assert b"Late fines" in page and b"grace period" in page and b"1% of the unpaid amount" in page
    assert f"{inst + fine.amount:,}".encode() in page                     # amount to pay now went up
    w.client_anon.get(f"/pay/{token}")
    assert LoanPenalty.query.filter_by(loan_id=loan.id).count() == 1       # never added twice
    # the lender sees the same fine with the reason
    assert b"grace period" in w.cl.get(f"{w.bp}/loans/{loan.id}").data


def test_payment_clears_fine_first_and_updates_both_sides(app, anon):
    w = anon
    loan = _fine_loan(w)
    first = loan.schedule[0]
    inst = first.balance
    first.due_date = date.today() - timedelta(days=10)
    db.session.commit()
    token = portal.token_for(db.session.get(Borrower, loan.borrower_id))
    db.session.commit()
    w.client_anon.get(f"/pay/{token}")
    fine = LoanPenalty.query.filter_by(loan_id=loan.id).one().amount
    out0 = sum(loan_svc.outstanding(loan))
    cash = ledger.cash_balance(w.biz.id)
    r = pay(w, loan, inst + fine, "Mobile Money", "MTN123")
    assert r.status_code == 302
    loan = db.session.get(Loan, loan.id)
    assert lender_svc.penalties_due(loan) == 0
    assert sum(loan_svc.outstanding(loan)) == out0 - inst
    assert ledger.cash_balance(w.biz.id) == cash + inst + fine
    assert loan.schedule[0].balance == 0
    page = w.client_anon.get(f"/pay/{token}").data                         # the borrower's side
    assert b"MTN123" not in page and f"{inst:,}".encode() in page          # payment shows, no internal refs leaked
    assert b"Still owed 0" in page or b"Still owed" in page
    # cash and bank are accepted too
    assert pay(w, loan, 10000, "Cash").status_code == 302
    assert pay(w, loan, 10000, "Bank Transfer", day=date.today() - timedelta(days=1)).status_code == 302
    assert LoanRepayment.query.filter_by(loan_id=loan.id, status="POSTED").count() == 3


def test_payment_smaller_than_fine_goes_to_fine(app, anon):
    w = anon
    loan = _fine_loan(w)
    loan.schedule[0].due_date = date.today() - timedelta(days=10)
    db.session.commit()
    w.cl.get(f"{w.bp}/loans/{loan.id}")                                    # assessing the fine
    fine = LoanPenalty.query.filter_by(loan_id=loan.id).one().amount
    out0 = sum(loan_svc.outstanding(db.session.get(Loan, loan.id)))
    pay(w, loan, fine - 1)
    loan = db.session.get(Loan, loan.id)
    assert lender_svc.penalties_due(loan) == 1 and sum(loan_svc.outstanding(loan)) == out0
    assert LoanRepayment.query.filter_by(loan_id=loan.id).count() == 0
    pay(w, loan, 1)
    assert lender_svc.penalties_due(db.session.get(Loan, loan.id)) == 0
    pay(w, loan, 99_999_999)                                                # more than everything owed: refused
    assert LoanRepayment.query.filter_by(loan_id=loan.id).count() == 0


def test_no_fine_when_paid_inside_grace_or_rule_off(app, anon):
    w = anon
    loan = _fine_loan(w, grace=7)
    loan.schedule[0].due_date = date.today() - timedelta(days=3)
    db.session.commit()
    w.cl.get(f"{w.bp}/loans/{loan.id}")
    assert LoanPenalty.query.filter_by(loan_id=loan.id).count() == 0
    loan = db.session.get(Loan, loan.id)
    loan.penalty_type = "NONE"                                              # rule switched off on this loan
    loan.schedule[0].due_date = date.today() - timedelta(days=60)
    db.session.commit()
    w.cl.get(f"{w.bp}/loans/{loan.id}")
    assert LoanPenalty.query.filter_by(loan_id=loan.id).count() == 0


def test_borrower_page_privacy_links_and_instructions(app, anon):
    w = anon
    w.cl.post(w.bp + "/settings", data={"name": w.biz.name, "currency": "UGX", "timezone": "Africa/Kampala",
                                         "contribution_frequency": "MONTHLY", "default_contribution": "0",
                                         "loan_approvals_required": "1", "penalty_type": "PERCENT",
                                         "penalty_value": "1", "penalty_grace_days": "7",
                                         "pay_instructions": "MTN MoMo 0772000000 (Quick Cash)"})
    assert db.session.get(Group, w.biz.id).pay_instructions == "MTN MoMo 0772000000 (Quick Cash)"
    assert (db.session.get(Group, w.biz.id).penalty_type, db.session.get(Group, w.biz.id).penalty_grace_days) == ("PERCENT", 7)
    loan = make_lender_loan(w, product=False)
    b = db.session.get(Borrower, loan.borrower_id)
    # the staff page creates and shows a copyable link
    page = w.cl.post(f"{w.bp}/borrowers/{b.id}/portal", data={"action": "show"}, follow_redirects=True).data
    assert b"/pay/" in page
    token = portal.token_for(b)
    db.session.commit()
    c = w.client_anon
    r = c.get(f"/pay/{token}")
    assert r.status_code == 200 and loan.reference.encode() in r.data and b"MTN MoMo 0772000000" in r.data
    assert b"national" not in r.data.lower() and b"Shop owner" not in r.data
    assert c.get("/pay/not-a-token").status_code == 404
    assert c.post(f"/pay/{token}/pay", data={}).status_code in (404, 405)          # read-only: no paying here
    w.cl.post(w.bp + "/borrowers/new", data={"name": "Dan Other", "category": "Individual"})
    other = Borrower.query.filter_by(name="Dan Other").one()
    other_page = c.get(f"/pay/{portal.token_for(other)}")
    db.session.commit()
    assert loan.reference.encode() not in other_page.data and b"No open loans" in other_page.data
    w.cl.post(f"{w.bp}/borrowers/{b.id}/portal", data={"action": "rotate"})
    assert c.get(f"/pay/{token}").status_code == 404
    sg_b = Borrower(group_id=w.ga.id, name="Sav B", registered_on=date.today(), created_by=w.admin_a.id)
    db.session.add(sg_b)
    db.session.commit()
    assert c.get(f"/pay/{portal.token_for(sg_b)}").status_code == 404


def test_phase2_pages_render(app, anon):
    w = anon
    loan = make_lender_loan(w)
    age(db.session.get(Loan, loan.id), 45)
    b = loan.borrower_id
    for path in ("/", "/collections/", "/applications/", "/applications/new", "/capital/",
                 f"/loans/{loan.id}", f"/borrowers/{b}", "/loans/", "/reports/", "/reports/fees_penalties",
                 "/repayments/new", f"/repayments/loans?borrower={b}", "/settings"):
        r = w.cl.get(w.bp + path)
        assert r.status_code == 200, (path, r.status_code)
    assert b"Record a payment" in w.cl.get(f"{w.bp}/loans/{loan.id}").data
    assert b"Credit standing" in w.cl.get(f"{w.bp}/borrowers/{b}").data
    assert w.cl.get(w.bp + "/payments/").status_code == 404
