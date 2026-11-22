"""Late-fee policy: 7-day grace, 5% per 7-day block, 100% cap; admin alerts, tracking log, PDF notice."""
import csv
import io
from datetime import date, timedelta

import pytest

from app.extensions import db
from app.models import LateFeeTracker, Notification
from app.services import late_fees
from tests.test_lender import biz, make_lender_loan  # noqa: F401  (fixtures)

D = date(2026, 1, 1)


# ---------------------------------------------------------------- the formula
@pytest.mark.parametrize("days,blocks,fine,flag", [
    (-3, 0, 0, "Not yet due"), (0, 0, 0, "Not yet due"), (1, 0, 0, "Grace Period"), (7, 0, 0, "Grace Period"),
    (8, 0, 0, "Active Overdue - No Fine Yet"), (13, 0, 0, "Active Overdue - No Fine Yet"),
    (14, 1, 2_500, "Active Overdue - Tier 1"), (20, 1, 2_500, "Active Overdue - Tier 1"),
    (21, 2, 5_000, "Active Overdue - Tier 2"), (35, 4, 10_000, "Active Overdue - Tier 4"),
    (146, 19, 47_500, "Active Overdue - Tier 19"), (147, 20, 50_000, "Max Fine Cap Reached"),
    (400, 56, 50_000, "Max Fine Cap Reached"),
])
def test_completed_block_mode(days, blocks, fine, flag):
    a = late_fees.assess(50_000, D, D + timedelta(days=days), "COMPLETED")
    assert (a["blocks"], a["fine"], a["flag"]) == (blocks, fine, flag)
    assert a["total"] == 50_000 + fine and a["fine"] <= 50_000


@pytest.mark.parametrize("days,blocks,fine", [(7, 0, 0), (8, 1, 2_500), (14, 1, 2_500), (15, 2, 5_000), (141, 20, 50_000)])
def test_started_block_mode_is_ceil(days, blocks, fine):
    a = late_fees.assess(50_000, D, D + timedelta(days=days), "STARTED")
    assert (a["blocks"], a["fine"]) == (blocks, fine)


def test_dates_tier_and_cap():
    a = late_fees.assess(50_000, D, D + timedelta(days=15), "COMPLETED")
    assert a["grace_end"] == D + timedelta(days=7) and a["tier_pct"] == 5
    assert a["next_increase"] == D + timedelta(days=21)                 # block 2 completes on day 21
    assert late_fees.assess(50_000, D, D + timedelta(days=15), "STARTED")["next_increase"] == D + timedelta(days=22)
    capped = late_fees.assess(50_000, D, D + timedelta(days=300))
    assert capped["capped"] and capped["tier_pct"] == 100 and capped["next_increase"] is None


def test_rounding_to_whole_currency_units():
    assert late_fees.assess(1_001, D, D + timedelta(days=14), "COMPLETED")["fine"] == 50      # 50.05 -> 50
    assert late_fees.assess(1_010, D, D + timedelta(days=14), "COMPLETED")["fine"] == 51      # 50.5 -> 51


# ---------------------------------------------------------------- tracking log, alerts, notice
def _overdue_loan(biz, days_late):
    loan = make_lender_loan(biz, product=False)
    s0 = loan.schedule[0]
    s0.due_date = date.today() - timedelta(days=days_late)
    db.session.commit()
    return loan, s0


def test_tracking_log_has_the_exact_columns(app, biz):
    loan, s0 = _overdue_loan(biz, 20)
    html = biz.cl.get(biz.bp + "/overdue/").data.decode()
    head = html.split("<thead>")[1].split("</thead>")[0]
    cols = [c.split(">")[1].split("<")[0] for c in head.split("<th")[1:]]
    assert cols == ["Loan ID", "Borrower Name", "Due Date", "Days Overdue", "Principal Due", "Penalty %", "Fine Accrued",
                    "Total Owed", "Last Notified"]
    fine = round(s0.balance * 0.05)
    assert loan.reference in html and "Carl Trader" in html and "5%" in html and f"{fine:,}" in html
    assert f"{s0.balance + fine:,}" in html and "Active Overdue - Tier 1" in html
    r = biz.cl.get(biz.bp + "/overdue/?export=csv")
    rows = list(csv.reader(io.StringIO(r.data.decode())))
    assert rows[0] == late_fees.LOG_COLUMNS and rows[1][0] == loan.reference and rows[1][5] == "5%"


def test_admin_alert_sent_once_per_new_penalty_block(app, biz):
    loan, s0 = _overdue_loan(biz, 15)                       # day 15 -> billable 8 -> block 1
    today = date.today()
    assert late_fees.run_daily(biz.biz, today) == 1
    db.session.commit()
    alerts = Notification.query.filter_by(kind="LATE_FEE").all()
    per_alert = len(alerts)
    assert per_alert >= 1
    n = alerts[0]
    for part in ("Carl Trader", loan.reference, "Original due date", "Days overdue: 15", "Original late amount", "Penalty tier: 5%",
                 "Accumulated fine", "New total balance due", "Status: Active Overdue - Tier 1"):
        assert part in (n.title + "\n" + n.body), part
    assert n.link.endswith(f"/overdue/{s0.id}/notice.pdf")
    t = LateFeeTracker.query.filter_by(schedule_id=s0.id).one()
    assert t.last_block == 1 and t.last_notified_at is not None
    assert late_fees.run_daily(biz.biz, today + timedelta(days=1)) == 0          # day 16: same block, no second alert
    assert late_fees.run_daily(biz.biz, today + timedelta(days=5)) == 0          # day 20: still block 1
    assert late_fees.run_daily(biz.biz, today + timedelta(days=6)) == 1          # day 21: block 2 -> new alert
    assert late_fees.run_daily(biz.biz, today + timedelta(days=6)) == 0          # and only once
    db.session.commit()
    assert Notification.query.filter_by(kind="LATE_FEE").count() == 2 * per_alert
    assert LateFeeTracker.query.filter_by(schedule_id=s0.id).one().last_block == 2
    assert "Never" not in biz.cl.get(biz.bp + "/overdue/").data.decode().split("<tbody>")[1]   # Last Notified is filled in


def test_no_alert_during_grace_period(app, biz):
    _overdue_loan(biz, 6)
    assert late_fees.run_daily(biz.biz, date.today()) == 0
    html = biz.cl.get(biz.bp + "/overdue/").data.decode()
    assert "Grace Period" in html and LateFeeTracker.query.count() == 0


def test_cap_alert_flag_and_total(app, biz):
    loan, s0 = _overdue_loan(biz, 200)
    assert late_fees.run_daily(biz.biz, date.today()) == 1
    n = Notification.query.filter_by(kind="LATE_FEE").first()
    assert "Max Fine Cap Reached" in n.title and "Penalty tier: 100%" in n.body
    row = late_fees.log_rows(biz.biz, date.today())[0]
    assert row["fine"] == s0.balance and row["total"] == 2 * s0.balance


def test_notice_pdf_has_all_required_fields(app, biz):
    pypdf = pytest.importorskip("pypdf")
    loan, s0 = _overdue_loan(biz, 20)
    biz.biz.pay_instructions = "Send mobile money to 0772 000 000 (Quick Cash Ltd)."
    db.session.commit()
    r = biz.cl.get(f"{biz.bp}/overdue/{s0.id}/notice.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data[:4] == b"%PDF"
    text = " ".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(r.data)).pages)
    text = " ".join(text.split())
    for needle in ("Overdue Loan & Late Fine Notice", "Company Name", "Quick Cash Ltd", "Notice Date", "Loan Reference ID", loan.reference,
                   "Borrower Details", "Full Name", "Carl Trader", "Contact Info", "Original Repayment Amount", "Original Due Date",
                   "Grace Period End Date (+7 Days)", "Current Days Past Due", "20 day(s)", "Late Fine Rate Applied",
                   "5% per 7-day cycle post-grace", "Accumulated Late Fine", "TOTAL AMOUNT PAYABLE", "Policy Reminder",
                   "5%", "every 7 days", "capped at 100%", "Payment Instructions", "Send mobile money to 0772 000 000",
                   "settle the total amount payable immediately", "next 7-day penalty increase"):
        assert needle in text, needle
    fine = round(s0.balance * 0.05)
    assert f"{s0.balance + fine:,}" in text


def test_notice_404_when_not_overdue(app, biz):
    loan = make_lender_loan(biz, product=False)
    assert biz.cl.get(f"{biz.bp}/overdue/{loan.schedule[0].id}/notice.pdf").status_code == 404


def test_access_control(app, biz):
    loan, s0 = _overdue_loan(biz, 20)
    for url in (biz.bp + "/overdue/", f"{biz.bp}/overdue/{s0.id}/notice.pdf"):
        assert biz.cs.get(url).status_code == 403                         # plain staff
        assert biz.ca.get(url).status_code == 404                         # another group's admin
        assert app.test_client().get(url).status_code in (302, 401)       # signed out


def test_dashboard_triggers_the_alert_once_a_day(app, biz):
    _overdue_loan(biz, 20)
    assert Notification.query.filter_by(kind="LATE_FEE").count() == 0
    biz.cl.get(biz.bp + "/")
    n = Notification.query.filter_by(kind="LATE_FEE").count()
    assert n >= 1
    biz.cl.get(biz.bp + "/")
    assert Notification.query.filter_by(kind="LATE_FEE").count() == n


def test_cli_command_runs(app, biz):
    _overdue_loan(biz, 20)
    res = app.test_cli_runner().invoke(args=["late-fees"])
    assert res.exit_code == 0 and "Late-fee alerts sent" in res.output


# ---------------------------------------------------------------- loan-level fine statement (before / after + PDF)
def _pdf_text(data):
    pypdf = pytest.importorskip("pypdf")
    return " ".join(" ".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(data)).pages).split())


def test_statement_before_fine_after(app, biz):
    loan, s0 = _overdue_loan(biz, 20)                        # day 20 -> one completed block -> 5%
    stmt = late_fees.statement(biz.biz, loan, date.today())
    fine = round(s0.balance * 0.05)
    assert stmt["principal_before"] == s0.balance and stmt["fine"] == fine
    assert stmt["principal_after"] == s0.balance + fine
    assert stmt["balance_after"] == stmt["balance_before"] + fine and stmt["longest_days"] == 20
    assert len(stmt["lines"]) == 1 and stmt["lines"][0]["blocks"] == 1 and stmt["next_increase"] == s0.due_date + timedelta(days=21)


def test_statement_matches_the_overdue_log(app, biz):
    loan, s0 = _overdue_loan(biz, 40)
    row = late_fees.log_rows(biz.biz, date.today())[0]
    stmt = late_fees.statement(biz.biz, loan, date.today())
    assert (stmt["principal_before"], stmt["fine"], stmt["principal_after"]) == (row["amount"], row["fine"], row["total"])


def test_statement_none_when_nothing_overdue(app, biz):
    loan = make_lender_loan(biz, product=False)
    assert late_fees.statement(biz.biz, loan, date.today()) is None


def test_statement_in_grace_period_has_no_fine(app, biz):
    loan, s0 = _overdue_loan(biz, 5)
    stmt = late_fees.statement(biz.biz, loan, date.today())
    assert stmt["fine"] == 0 and stmt["principal_after"] == stmt["principal_before"] == s0.balance


def test_statement_pdf_explains_the_fine(app, biz):
    loan, s0 = _overdue_loan(biz, 20)
    biz.biz.pay_instructions = "Send mobile money to 0772 000 000 (Quick Cash Ltd)."
    db.session.commit()
    r = biz.cl.get(f"{biz.bp}/overdue/loan/{loan.id}/statement.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data[:4] == b"%PDF"
    text, fine = _pdf_text(r.data), round(s0.balance * 0.05)
    for needle in ("Overdue Loan Fine Statement", loan.reference, "Carl Trader", "Initial principal due (before the fine)",
                   "Late fine added", "NEW PRINCIPAL DUE (after the fine)", "Why this fine applies", "7-day grace period",
                   "capped at 100%", "How each fine was calculated", "The working, instalment by instalment", "What happens next",
                   "How to pay", "Send mobile money to 0772 000 000", f"{s0.balance:,}", f"{fine:,}", f"{s0.balance + fine:,}"):
        assert needle in text, needle


def test_statement_404_when_not_overdue(app, biz):
    loan = make_lender_loan(biz, product=False)
    assert biz.cl.get(f"{biz.bp}/overdue/loan/{loan.id}/statement.pdf").status_code == 404


def test_statement_access_control(app, biz):
    loan, _ = _overdue_loan(biz, 20)
    url = f"{biz.bp}/overdue/loan/{loan.id}/statement.pdf"
    assert biz.cs.get(url).status_code == 403                         # plain staff
    assert biz.ca.get(url).status_code == 404                         # another group's admin
    assert app.test_client().get(url).status_code in (302, 401)       # signed out


def test_loan_page_shows_fine_card_only_when_overdue(app, biz):
    loan = make_lender_loan(biz, product=False)
    assert "Overdue fine statement" not in biz.cl.get(f"{biz.bp}/loans/{loan.id}").data.decode()
    s0 = loan.schedule[0]
    s0.due_date = date.today() - timedelta(days=20)
    db.session.commit()
    html = biz.cl.get(f"{biz.bp}/loans/{loan.id}").data.decode()
    fine = round(s0.balance * 0.05)
    for needle in ("Overdue fine statement", "Initial principal (before fine)", "New principal (after fine)", "Fine statement (PDF)",
                   f"{s0.balance:,}", f"{fine:,}", f"{s0.balance + fine:,}"):
        assert needle in html, needle
