"""Loan summary: principal, interest, repaid, fines over the period, and the total with fines, at every stage of a loan."""
from datetime import date, timedelta
from types import SimpleNamespace as NS

from app.services.loan_summary import summary

D0 = date(2026, 1, 1)


def sched(n, due, paid_p=0, paid_i=0, sid=None):
    return NS(id=sid or n, number=n, due_date=due, principal_due=200_000, interest_due=20_000, principal_paid=paid_p,
              interest_paid=paid_i, total_paid=paid_p + paid_i, balance=220_000 - paid_p - paid_i)


def rep(pay_date, *allocs):
    return NS(id=len(allocs) + pay_date.toordinal(), status="POSTED", payment_date=pay_date,
              allocations=[NS(schedule_id=s, principal=p, interest=i) for s, p, i in allocs])


def fine(deadline, paid_on, amount, status, late=10, settled=None):
    return NS(status=status, paid_on=paid_on, deadline=deadline, days_late=late, late_amount=220_000, amount=amount,
              settled_on=settled)


def loan(schedule, repayments, fines=(), penalties=(), status="FULLY_REPAID"):
    return NS(disbursement_tx_id=1, status=status, principal=600_000, interest_rebate=0, disbursement_date=D0,
              schedule=schedule, repayments=list(repayments), fines=list(fines), penalties=list(penalties))


def repaid_loan():
    d1, d2, d3 = D0 + timedelta(days=30), D0 + timedelta(days=60), D0 + timedelta(days=90)
    s = [sched(1, d1, 200_000, 20_000), sched(2, d2, 200_000, 20_000), sched(3, d3, 200_000, 20_000)]
    r = [rep(d1, (1, 200_000, 20_000)), rep(d2 + timedelta(days=10), (2, 200_000, 20_000)),
         rep(d3 + timedelta(days=4), (3, 200_000, 20_000))]
    f = [fine(d2, d2 + timedelta(days=10), 5_000, "PAID", 10, d2 + timedelta(days=10)),
         fine(d3, d3 + timedelta(days=4), 3_000, "WAIVED", 4)]
    return loan(s, r, f), (d1, d2, d3)


def test_repaid_loan_shows_everything_after_the_fact():
    ln, (d1, d2, d3) = repaid_loan()
    x = summary(ln, D0 + timedelta(days=400))
    assert (x["principal"], x["interest"], x["repaid"]) == (600_000, 60_000, 660_000)
    assert (x["fines_charged"], x["fines_waived"], x["fines_paid"], x["fines_unpaid"]) == (8_000, 3_000, 5_000, 0)
    assert x["total_with_fines"] == 665_000                     # principal + interest + fine actually due (5,000)
    assert x["balance"] == 0 and x["finished"] and x["days_taken"] == 94
    assert x["late_instalments"] == 2 and x["fine_pct_of_loan"] == 0.8
    assert [r["days_late"] for r in x["lines"]] == [0, 10, 4]
    assert [r["status"] for r in x["lines"]] == ["Paid on time", "Paid late", "Paid late"]
    assert [r["fine"] for r in x["lines"]] == [0, 5_000, 0]     # the waived fine no longer counts against instalment 3
    assert [f["status"] for f in x["fines"]] == ["Paid", "Waived"]


def test_loan_without_fines():
    d1 = D0 + timedelta(days=30)
    ln = loan([sched(1, d1, 200_000, 20_000)], [rep(d1, (1, 200_000, 20_000))])
    ln.principal = 200_000
    x = summary(ln, d1 + timedelta(days=5))
    assert x["fines"] == [] and x["fines_charged"] == 0 and x["total_with_fines"] == 220_000 and x["balance"] == 0


def test_running_loan_counts_fine_building_up_and_what_is_still_owed():
    d1, d2, d3 = D0 + timedelta(days=30), D0 + timedelta(days=60), D0 + timedelta(days=90)
    s = [sched(1, d1, 200_000, 20_000), sched(2, d2), sched(3, d3)]
    ln = loan(s, [rep(d1, (1, 200_000, 20_000))], [], status="OVERDUE")
    x = summary(ln, d2 + timedelta(days=20), accruing=11_000)
    assert x["total_with_fines"] == 660_000 + 11_000 and x["accruing"] == 11_000
    assert x["balance"] == 660_000 + 11_000 - 220_000          # nothing but instalment 1 paid
    assert [r["status"] for r in x["lines"]] == ["Paid on time", "Overdue", "Not yet due"]
    assert x["lines"][1]["days_late"] == 20 and not x["finished"] and x["days_taken"] is None


def test_unpaid_fine_stays_owed_and_a_lenders_penalties_are_included():
    d1 = D0 + timedelta(days=30)
    s = [sched(1, d1, 200_000, 20_000)]
    ln = loan(s, [rep(d1 + timedelta(days=9), (1, 200_000, 20_000))],
              [fine(d1, d1 + timedelta(days=9), 4_000, "UNPAID", 9)], status="FULLY_REPAID")
    ln.principal = 200_000
    ln.penalties = [NS(schedule_id=1, amount=6_000, paid=2_000, waived=1_000, assessed_on=d1 + timedelta(days=9))]
    x = summary(ln, d1 + timedelta(days=30))
    assert x["fines_charged"] == 10_000 and x["fines_paid"] == 2_000 and x["fines_waived"] == 1_000
    assert x["fines_unpaid"] == 7_000                              # 10,000 - 1,000 waived - 2,000 paid
    assert x["total_with_fines"] == 220_000 + 9_000 and x["balance"] == 7_000
    assert {f["status"] for f in x["fines"]} == {"Unpaid", "Part paid"}


def test_early_settlement_rebate_and_reversed_repayments_and_never_lent():
    d1 = D0 + timedelta(days=30)
    s = [sched(1, d1, 200_000, 20_000)]
    s[0].interest_due = 8_000                                       # interest reduced when settled early
    s[0].interest_paid, s[0].total_paid, s[0].balance = 8_000, 208_000, 0
    ln = loan(s, [rep(d1, (1, 200_000, 8_000)), NS(id=9, status="REVERSED", payment_date=d1, allocations=[])])
    ln.principal, ln.interest_rebate = 200_000, 12_000
    x = summary(ln, d1)
    assert x["interest"] == 8_000 and x["interest_rebate"] == 12_000 and x["balance"] == 0
    ln.disbursement_tx_id = None
    assert summary(ln, d1) is None
    ln.disbursement_tx_id, ln.status = 5, "PENDING_APPROVAL"
    assert summary(ln, d1) is None


def test_full_statement_pdf_builds_for_repaid_and_running_loans():
    from datetime import datetime
    from app.services import documents
    ln, (d1, d2, d3) = repaid_loan()
    ln.borrower = NS(name="Mary", phone="0700", location="x")
    ln.reference, ln.rate_bp, ln.rate_period, ln.method, ln.frequency = "LN-1", 500, "MONTH", "FLAT", "MONTHLY"
    ln.purpose, ln.total_interest = "Farm", 60_000
    for i, r in enumerate(ln.repayments):
        r.amount, r.principal_part, r.interest_part, r.receipt_no, r.payment_method = 220_000, 200_000, 20_000, f"RC-{i}", None
    grp = NS(name="Trinity", currency="UGX", currency_decimals=0, timezone="UTC", phone="", logo=None)
    x = summary(ln, D0 + timedelta(days=400))
    assert documents.loan_statement_pdf(grp, ln, x, datetime(2026, 6, 1), "Late payments: 2%").startswith(b"%PDF")
