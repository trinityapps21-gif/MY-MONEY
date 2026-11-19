import io
from datetime import date, timedelta

import pytest

from app.extensions import db
from app.groups import create_group_from_form
from app.models import (Borrower, BorrowerDocument, Collateral, Guarantor, Loan, LoanPenalty, LoanProduct,
                        Transaction)
from app.services import ledger, lender as lender_svc, loans as loan_svc
from tests.conftest import add_member, login, loan_form, make_user, png_bytes


@pytest.fixture()
def biz(app, world):
    """A lending business with an owner, a plain staff member, and starting capital."""
    owner = make_user("Lola Lender", "0772000101", "l@x.com")
    grp, err = create_group_from_form(owner, {"group_name": "Quick Cash Ltd", "currency": "UGX",
                                              "business_type": "LENDING"}, None)
    assert not err, err
    db.session.commit()
    staff = make_user("Sam Staff", "0772000102", "s@x.com")
    add_member(grp, staff)
    ledger.post(grp.id, "ADJUSTMENT", 20_000_000, "IN", date.today(), owner.id, "Starting capital", prefix="ADJ")
    db.session.commit()
    world.biz, world.cl, world.cs = grp, login(app, "l@x.com"), login(app, "s@x.com")
    world.bp = f"/g/{grp.id}"
    return world


def product_form(**kw):
    f = {"name": "Salary loan", "method": "FLAT", "rate": "2.5", "rate_period": "MONTH", "duration": 3,
         "duration_unit": "MONTH", "frequency": "MONTHLY", "min_amount": "100000", "max_amount": "5000000",
         "processing_fee_type": "PERCENT", "processing_fee_value": "5", "insurance_fee_type": "FLAT",
         "insurance_fee_value": "20000", "penalty_type": "FLAT", "penalty_value": "5000", "penalty_grace_days": 2,
         "active": "1"}
    f.update(kw)
    return f


def make_lender_loan(w, product=True, **kw):
    b = w.bp
    w.cl.post(b + "/borrowers/new", data={"name": "Carl Trader", "category": "Trader", "occupation": "Shop owner",
                                          "monthly_income": "800000", "next_of_kin_name": "Mary"})
    bid = Borrower.query.filter_by(group_id=w.biz.id).first().id
    if product:
        w.cl.post(b + "/products/new", data=product_form())
        kw["product_id"] = LoanProduct.query.filter_by(group_id=w.biz.id).first().id
    r = w.cl.post(b + "/loans/new", data=loan_form(bid, rate="2.5", principal="1000000", **kw))
    loan = Loan.query.filter_by(group_id=w.biz.id).first()
    assert loan is not None, r.data[:500]
    for a in ("approve", "disburse"):
        w.cl.post(f"{b}/loans/{loan.id}/action", data={"action": a})
    return db.session.get(Loan, loan.id)


# ---------------------------------------------------------------- business type
def test_business_type_is_fixed_and_separates_features(app, biz):
    assert biz.biz.business_type == "LENDING" and biz.ga.business_type == "SAVINGS_GROUP"
    assert biz.cl.get(biz.bp + "/savings/").status_code == 404          # no savings module for a lender
    assert biz.cl.get(biz.bp + "/products/").status_code == 200
    assert biz.ca.get(f"/g/{biz.ga.id}/products/").status_code == 404   # no products for a savings group
    assert biz.ca.get(f"/g/{biz.ga.id}/savings/").status_code == 200
    page = biz.cl.get(biz.bp + "/").data
    assert b"Loan Book" in page and b"Loan Products" in page and b"Savings" not in page.split(b"<main")[0]
    # settings cannot flip the type
    biz.cl.post(biz.bp + "/settings", data={"group_name": "Quick Cash Ltd", "currency": "UGX",
                                            "business_type": "SAVINGS_GROUP"})
    assert db.session.get(type(biz.biz), biz.biz.id).business_type == "LENDING"


def test_lender_staff_without_finance_rights_and_reports(app, biz):
    assert biz.cs.get(biz.bp + "/", follow_redirects=False).status_code == 302   # sent to the community
    idx = biz.cl.get(biz.bp + "/reports/").data
    assert b"Fees &amp; Penalties" in idx and b"Member Contributions" not in idx
    assert biz.cl.get(biz.bp + "/reports/fees_penalties").status_code == 200
    assert biz.cl.get(biz.bp + "/reports/member_contributions").status_code == 404
    assert biz.ca.get(f"/g/{biz.ga.id}/reports/fees_penalties").status_code == 404


# ---------------------------------------------------------------- rate cap
def test_rate_cap_for_lenders_only(app, biz):
    s = loan_svc.get_interest_settings()
    assert s.lender_cap_bp == 280
    for method, rate, period, ok in (("FLAT", 250, "MONTH", True), ("FLAT", 280, "MONTH", True),
                                     ("FLAT", 300, "MONTH", False), ("FLAT", 100, "WEEK", False),
                                     ("REDUCING", 3000, "YEAR", True), ("REDUCING", 4000, "YEAR", False)):
        if ok:
            loan_svc.validate_terms(method, rate, period, biz.biz)
        else:
            with pytest.raises(loan_svc.LoanError):
                loan_svc.validate_terms(method, rate, period, biz.biz)
    loan_svc.validate_terms("FLAT", 1000, "MONTH", biz.ga)   # a savings group is not affected by the lender cap
    biz.biz.rate_cap_bp = 200                                # the business can be stricter, never looser
    with pytest.raises(loan_svc.LoanError):
        loan_svc.validate_terms("FLAT", 250, "MONTH", biz.biz)
    biz.biz.rate_cap_bp = 500
    with pytest.raises(loan_svc.LoanError):
        loan_svc.validate_terms("FLAT", 300, "MONTH", biz.biz)
    r = biz.cl.post(biz.bp + "/products/new", data=product_form(name="Too dear", rate="9"))
    assert LoanProduct.query.filter_by(group_id=biz.biz.id).count() == 0 and r.status_code in (200, 302)


# ---------------------------------------------------------------- products, fees, ledger
def test_product_fees_collected_at_disbursement_and_reversed(app, biz):
    cash0 = ledger.cash_balance(biz.biz.id)
    loan = make_lender_loan(biz)
    assert loan.status == "ACTIVE"
    assert loan.fee_processing == 50_000 and loan.fee_insurance == 20_000          # 5% of 1,000,000 + flat
    assert (loan.penalty_type, loan.penalty_value, loan.penalty_grace_days) == ("FLAT", 5000, 2)
    assert ledger.cash_balance(biz.biz.id) == cash0 - 1_000_000 + 70_000
    fee_tx = db.session.get(Transaction, loan.fee_tx_id)
    assert fee_tx.tx_type == "FEE" and fee_tx.direction == "IN" and fee_tx.amount == 70_000
    assert ledger.totals(biz.biz.id)["fees"] == 70_000
    page = biz.cl.get(f"{biz.bp}/loans/{loan.id}").data
    assert b"Processing" in page and b"Guarantors" in page and b"Collateral" in page
    # editing the product later never changes the existing loan
    prod = db.session.get(LoanProduct, loan.product_id)
    biz.cl.post(f"{biz.bp}/products/{prod.id}", data=product_form(processing_fee_value="9"))
    assert db.session.get(Loan, loan.id).fee_processing == 50_000
    # reversing the disbursement also reverses the fees
    biz.cl.post(f"{biz.bp}/loans/{loan.id}/action", data={"action": "reverse_disbursement", "reason": "wrong amount"})
    assert ledger.cash_balance(biz.biz.id) == cash0
    assert ledger.totals(biz.biz.id)["fees"] == 0


def test_product_amount_limits_and_inactive(app, biz):
    biz.cl.post(biz.bp + "/products/new", data=product_form())
    prod = LoanProduct.query.filter_by(group_id=biz.biz.id).first()
    with pytest.raises(loan_svc.LoanError):
        loan_svc.charges(biz.biz, {"product_id": prod.id, "principal": 50_000})
    with pytest.raises(loan_svc.LoanError):
        loan_svc.charges(biz.biz, {"product_id": prod.id, "principal": 9_000_000})
    biz.cl.post(f"{biz.bp}/products/{prod.id}", data=product_form(active=""))
    with pytest.raises(loan_svc.LoanError):
        loan_svc.charges(biz.biz, {"product_id": prod.id, "principal": 500_000})
    other = LoanProduct(group_id=biz.ga.id, name="Elsewhere", rate_bp=100, created_by=biz.admin_a.id)
    db.session.add(other)
    db.session.commit()
    with pytest.raises(loan_svc.LoanError):   # a product from another business is never usable
        loan_svc.charges(biz.biz, {"product_id": other.id, "principal": 500_000})


# ---------------------------------------------------------------- penalties
def test_penalty_accrues_once_per_instalment_collects_and_waives(app, biz):
    loan = make_lender_loan(biz)
    first = loan.schedule[0]
    assert lender_svc.accrue_penalties(biz.biz, first.due_date + timedelta(days=2), loan) == 0   # inside grace
    assert lender_svc.accrue_penalties(biz.biz, first.due_date + timedelta(days=3), loan) == 1
    assert lender_svc.accrue_penalties(biz.biz, first.due_date + timedelta(days=9), loan) == 0   # not twice
    assert LoanPenalty.query.filter_by(loan_id=loan.id).one().amount == 5000
    later = loan.schedule[1].due_date + timedelta(days=5)
    assert lender_svc.accrue_penalties(biz.biz, later, loan) == 1
    assert lender_svc.penalties_due(db.session.get(Loan, loan.id)) == 10_000
    cash = ledger.cash_balance(biz.biz.id)
    r = biz.cl.post(f"{biz.bp}/loans/{loan.id}/penalty", data={"amount": "6000", "payment_method": "Cash"})
    assert r.status_code == 302
    loan = db.session.get(Loan, loan.id)
    assert lender_svc.penalties_due(loan) == 4000 and ledger.cash_balance(biz.biz.id) == cash + 6000
    biz.cl.post(f"{biz.bp}/loans/{loan.id}/penalty", data={"amount": "99999"})        # more than owed: refused
    assert ledger.cash_balance(biz.biz.id) == cash + 6000
    biz.cl.post(f"{biz.bp}/loans/{loan.id}/penalty", data={"action": "waive", "reason": ""})   # reason required
    assert lender_svc.penalties_due(db.session.get(Loan, loan.id)) == 4000
    biz.cl.post(f"{biz.bp}/loans/{loan.id}/penalty", data={"action": "waive", "reason": "Hospital stay"})
    assert lender_svc.penalties_due(db.session.get(Loan, loan.id)) == 0
    assert biz.cs.post(f"{biz.bp}/loans/{loan.id}/penalty", data={"amount": "1"}).status_code == 403
    assert ledger.totals(biz.biz.id)["penalties"] == 6000


def test_paid_instalment_is_not_penalised_and_savings_groups_never_are(app, biz):
    loan = make_lender_loan(biz)
    first = loan.schedule[0]
    biz.cl.post(f"{biz.bp}/repayments/new", data={"loan_id": loan.id, "amount": str(first.balance // 100 * 100 + 100),
                                                   "payment_date": date.today().isoformat(),
                                                   "payment_method": "Cash", "idempotency_key": "p1"})
    loan = db.session.get(Loan, loan.id)
    assert loan.schedule[0].balance <= 0
    lender_svc.accrue_penalties(biz.biz, first.due_date + timedelta(days=20), loan)
    assert all(p.schedule_id != first.id for p in db.session.get(Loan, loan.id).penalties)
    assert lender_svc.accrue_penalties(biz.ga, date.today() + timedelta(days=400)) == 0


# ---------------------------------------------------------------- KYC, guarantors, collateral
def test_borrower_kyc_documents_and_private_files(app, biz):
    loan = make_lender_loan(biz, product=False)
    b = Borrower.query.filter_by(group_id=biz.biz.id).first()
    assert (b.occupation, b.monthly_income, b.next_of_kin_name) == ("Shop owner", 800_000, "Mary")
    up = biz.cl.post(f"{biz.bp}/borrowers/{b.id}/documents", content_type="multipart/form-data",
                     data={"doc_type": "ID_FRONT", "note": "NIN", "file": (io.BytesIO(png_bytes()), "id.png")})
    assert up.status_code == 302
    doc = BorrowerDocument.query.filter_by(borrower_id=b.id).one()
    assert biz.cl.get(f"/files/{doc.file_id}").status_code == 200
    assert biz.cs.get(f"/files/{doc.file_id}").status_code in (403, 404)   # staff without finance rights
    assert biz.cb.get(f"/files/{doc.file_id}").status_code in (403, 404)   # other tenants
    assert b"NIN" in biz.cl.get(f"{biz.bp}/borrowers/{b.id}").data
    bad = biz.cl.post(f"{biz.bp}/borrowers/{b.id}/documents", content_type="multipart/form-data",
                      data={"doc_type": "OTHER", "file": (io.BytesIO(b"MZ not an image"), "x.exe")})
    assert BorrowerDocument.query.filter_by(borrower_id=b.id).count() == 1 and bad.status_code == 302
    biz.cl.post(f"{biz.bp}/borrowers/{b.id}/documents/{doc.id}/delete")
    assert b"NIN" not in biz.cl.get(f"{biz.bp}/borrowers/{b.id}").data
    assert loan.id


def test_guarantor_and_collateral_are_tenant_scoped(app, biz):
    loan = make_lender_loan(biz, product=False)
    b = biz.bp
    biz.cl.post(f"{b}/loans/{loan.id}/guarantors", content_type="multipart/form-data",
                data={"name": "Gina Guarantor", "phone": "0772555555", "relationship": "Sister",
                      "file": (io.BytesIO(png_bytes()), "g.png")})
    gr = Guarantor.query.filter_by(loan_id=loan.id).one()
    assert gr.file_id and gr.group_id == biz.biz.id
    biz.cl.post(f"{b}/loans/{loan.id}/collateral", content_type="multipart/form-data",
                data={"kind": "Land title", "estimated_value": "3000000", "description": "Plot 12"})
    c = Collateral.query.filter_by(loan_id=loan.id).one()
    assert c.estimated_value == 3_000_000 and c.status == "HELD"
    biz.cl.post(f"{b}/loans/{loan.id}/collateral/{c.id}/status", data={"status": "RELEASED"})
    assert db.session.get(Collateral, c.id).status == "RELEASED"
    assert biz.cl.post(f"{b}/loans/{loan.id}/collateral/{c.id}/status", data={"status": "BOGUS"}).status_code == 400
    page = biz.cl.get(f"{b}/loans/{loan.id}").data
    assert b"Gina Guarantor" in page and b"Land title" in page
    # another business cannot touch them
    assert biz.cb.post(f"/g/{biz.gb.id}/loans/{loan.id}/guarantors", data={"name": "Intruder"}).status_code == 404
    assert biz.cs.post(f"{b}/loans/{loan.id}/guarantors", data={"name": "Nope"}).status_code == 403
    biz.cl.post(f"{b}/loans/{loan.id}/guarantors/{gr.id}/delete")
    assert Guarantor.query.filter_by(loan_id=loan.id).count() == 0


# ---------------------------------------------------------------- settings / PWA
def test_lender_settings_and_platform_cap(app, biz):
    r = biz.cl.post(biz.bp + "/settings", data={"group_name": "Quick Cash Ltd", "currency": "UGX", "rate_cap": "2.5",
                                                "penalty_type": "PERCENT", "penalty_value": "4",
                                                "penalty_grace_days": "3", "loan_approvals_required": "1"})
    g = db.session.get(type(biz.biz), biz.biz.id)
    assert (g.rate_cap_bp, g.penalty_type, g.penalty_value, g.penalty_grace_days) == (250, "PERCENT", 400, 3), r.data[:300]
    from app.models import InterestSettings
    assert loan_svc.platform_lender_cap() == 280                      # default: 2.8% per month
    db.session.get(InterestSettings, 1).lender_cap_bp = 200
    db.session.commit()
    assert loan_svc.platform_lender_cap() == 200


def test_pwa_manifest_is_linked(app, world):
    assert b'rel="manifest"' in app.test_client().get("/login").data


def test_forms_render_plain_numbers(app, biz):
    loan = make_lender_loan(biz)
    first = loan.schedule[0]
    lender_svc.accrue_penalties(biz.biz, first.due_date + timedelta(days=5), loan)
    prod = LoanProduct.query.filter_by(group_id=biz.biz.id).first()
    assert b'name="min_amount" value="100000"' in biz.cl.get(f"{biz.bp}/products/{prod.id}").data
    assert biz.cl.get(biz.bp + "/products/new").status_code == 200
    page = biz.cl.get(f"{biz.bp}/loans/{loan.id}").data
    assert b"5,000" in page and b"5000.0" not in page and b"Late fine" in page
    assert b'name="monthly_income" value="800000"' in biz.cl.get(
        f"{biz.bp}/borrowers/{Borrower.query.filter_by(group_id=biz.biz.id).first().id}").data
    assert biz.cl.get(biz.bp + "/settings").status_code == 200
