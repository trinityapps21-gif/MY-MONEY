"""Interest page + the rule that a loan's total interest is only shown once the loan is fully repaid."""
from datetime import date, timedelta

from app.extensions import db
from app.models import Loan
from app.services import interest_report as ir, loans as loan_svc
from tests.test_lender import biz, make_lender_loan  # noqa: F401  (fixtures)


def _pay(biz, loan, amount, on=None):
    from app.models import User
    user = User.query.filter_by(email="l@x.com").first()
    loan_svc.record_repayment(biz.biz, loan, user, amount, on or date.today(), skip_recent_check=True)
    db.session.commit()


def _html(r):
    return r.data.decode()


def test_overview_figures_and_dates(app, biz):
    loan = make_lender_loan(biz, product=False)          # 1,000,000 at 2.5%/month flat, 3 months -> 75,000 interest
    today = date.today()
    ov = ir.overview(biz.biz, today)
    assert ov["received"] == 0 and ov["expected"] == 75_000 and ov["overdue"] == 0 and ov["incoming"] == 75_000
    assert ov["next_date"] == loan.schedule[0].due_date
    first_interest = loan.schedule[0].interest_due
    _pay(biz, loan, loan.schedule[0].total_due)           # first instalment in full
    ov = ir.overview(biz.biz, today)
    assert ov["received"] == first_interest and ov["expected"] == 75_000 - first_interest
    assert ov["received"] + ov["expected"] == 75_000


def test_overdue_vs_incoming_split(app, biz):
    loan = make_lender_loan(biz, product=False)
    s0 = loan.schedule[0]
    s0.due_date = date.today() - timedelta(days=10)       # first instalment is now late
    db.session.commit()
    ov = ir.overview(biz.biz, date.today())
    assert ov["overdue"] == s0.interest_due and ov["incoming"] == 75_000 - s0.interest_due
    assert ov["expected"] == ov["overdue"] + ov["incoming"]


def test_page_renders_every_view_with_dates(app, biz):
    loan = make_lender_loan(biz, product=False)
    url = biz.bp + "/interest/"
    page = _html(biz.cl.get(url))
    assert "Received interest" in page and "Expected interest" in page and "Incoming interest" in page
    assert loan.reference in page and loan.schedule[0].due_date.strftime("%d %b %Y") in page
    for view in ("upcoming", "overdue", "received", "all", "loans", "nonsense"):
        assert biz.cl.get(f"{url}?view={view}").status_code == 200, view
    assert "Nothing here" in _html(biz.cl.get(url + "?view=received"))      # nothing received yet
    _pay(biz, loan, loan.schedule[0].total_due)
    assert loan.reference in _html(biz.cl.get(url + "?view=received"))


def test_interest_page_is_officials_only_and_group_scoped(app, biz):
    assert biz.cs.get(biz.bp + "/interest/").status_code == 403           # plain staff member
    assert biz.ca.get(biz.bp + "/interest/").status_code == 404           # another group's admin
    assert app.test_client().get(biz.bp + "/interest/").status_code in (302, 401)


def test_total_interest_hidden_until_loan_fully_repaid(app, biz):
    loan = make_lender_loan(biz, product=False)
    detail = biz.bp + f"/loans/{loan.id}"
    page = _html(biz.cl.get(detail))
    assert "Interest received so far" in page and "fully repaid" in page
    assert ">Total interest<" not in page.replace("\n", "")
    assert loan_svc.portfolio(biz.biz, date.today())["interest_earned"] == 0
    assert "75,000" not in _html(biz.cl.get(biz.bp + "/loans/")) .split("Interest earned")[1][:200]
    # the "By loan" tab never reveals the running total either
    assert "After full repayment" in _html(biz.cl.get(biz.bp + "/interest/?view=loans"))

    for s in list(loan.schedule):                                           # borrower finishes the loan
        _pay(biz, db.session.get(Loan, loan.id), s.balance)
    loan = db.session.get(Loan, loan.id)
    assert loan.status == "FULLY_REPAID"
    assert loan_svc.portfolio(biz.biz, date.today())["interest_earned"] == 75_000
    page = _html(biz.cl.get(detail))
    assert "Total interest" in page and "Loan completed" in page
    assert "75,000" in _html(biz.cl.get(biz.bp + "/interest/?view=loans"))
    ov = ir.overview(biz.biz, date.today())
    assert ov["received"] == 75_000 and ov["expected"] == 0


def test_disbursements_report_shows_received_not_total(app, biz):
    make_lender_loan(biz, product=False)
    r = biz.cl.get(biz.bp + "/reports/loan_disbursements")
    assert r.status_code == 200
    body = _html(r)
    assert "Interest received so far" in body and "Interest agreed" not in body
