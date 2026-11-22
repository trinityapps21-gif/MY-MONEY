"""Late-fee tracking: monitor overdue repayments, work out the fine, alert administrators.

THE TERMS BELONG TO THE LOAN. Each group sets its own late-fine terms in Group rules, and every loan freezes a copy of them
when it is created (services/fine_terms.py). A loan is always assessed under its own terms, so changing the group's rules
never changes what an existing loan owes. The defaults a new group starts with are:
  * Grace period: no fine from the due date through day 7 after it.
  * After the grace period: 5% of the original late amount for every 7-day block.
        days_overdue   = today - due_date
        billable_days  = days_overdue - grace_days
        blocks         = 0 if billable_days <= 0, else floor(billable_days / period_days)   (mode COMPLETED, the default)
                                                       or ceil(billable_days / period_days)    (mode STARTED)
        raw_fine       = late_amount * rate * blocks          (PERCENT)     or     fixed_amount * blocks    (FIXED)
        final_fine     = min(raw_fine, late_amount)           (when the terms are capped, the default)
        total_owed     = late_amount + final_fine
  * Whenever a borrower enters a new penalty block, every administrator gets one alert.

Set LATE_FEE_BLOCK_MODE=STARTED to charge at the START of each block (ceil) instead of when it is completed.
These figures are informational: this module never posts to the ledger. Collecting money still goes through the
normal repayment screens. The late amount is what is still unpaid on the instalment (principal + interest)."""
import math
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from flask import current_app, has_app_context

from app.extensions import db
from app.models import Borrower, Loan, LoanRepaymentSchedule as S, LateFeeTracker, utcnow
from app.services import fine_terms, notify
from app.services.groupcfg import get_settings
from app.services.loans import ACTIVE_STATES, outstanding
from app.services.money import fmt
from app.services.timeutil import to_local


def block_mode() -> str:
    mode = (current_app.config.get("LATE_FEE_BLOCK_MODE") if has_app_context() else None) or "COMPLETED"
    mode = mode.upper()
    return mode if mode in ("COMPLETED", "STARTED") else "COMPLETED"


def assess(amount: int, due_date, today, mode: str | None = None, terms=None) -> dict:
    """Apply late-fine terms to one late amount (minor units). Pure function: no database access.

    `terms` is whatever carries the fine_* terms, normally the LOAN being assessed (its own frozen terms). Called without
    terms it uses the defaults a new group starts with (7-day grace, 5% every 7 days, capped at the late amount)."""
    t = fine_terms.terms(terms if terms is not None else fine_terms.DEFAULTS)
    mode = mode or block_mode()
    grace, period = t.fine_grace_days, max(1, t.fine_period_days)
    charges = not fine_terms.is_free(t)
    days = (today - due_date).days
    billable = days - grace
    if billable <= 0:
        blocks = 0
    elif mode == "STARTED":
        blocks = math.ceil(billable / period)
    else:
        blocks = billable // period
    if t.fine_type == "FIXED":
        raw = Decimal(t.fine_fixed) * blocks
    else:
        raw = (Decimal(amount) * t.fine_rate_bp * blocks / Decimal(10000)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    fine = int(min(raw, Decimal(amount))) if t.fine_capped else int(raw)
    capped = bool(t.fine_capped and charges and blocks > 0 and raw >= amount)
    if t.fine_type == "FIXED":
        tier_bp = round(fine * 10000 / amount) if amount else 0          # the fixed fee as a share of the late amount
    else:
        tier_bp = t.fine_rate_bp * blocks
        if t.fine_capped:
            tier_bp = min(tier_bp, 10000)
    # first day the NEXT block starts charging (None once the cap is reached, or when the terms charge nothing)
    if capped or not charges:
        next_date = None
    elif mode == "STARTED":
        next_date = due_date + timedelta(days=grace + period * blocks + 1)
    else:
        next_date = due_date + timedelta(days=grace + period * (blocks + 1))
    if days <= 0:
        flag = "Not yet due"
    elif days <= grace:
        flag = "Grace Period"
    elif not charges:
        flag = "Overdue - No Late Fine"            # this loan's terms charge no fine
    elif blocks == 0:
        flag = "Active Overdue - No Fine Yet"      # grace is over, the first full period has not finished
    elif capped:
        flag = "Max Fine Cap Reached"
    else:
        flag = f"Active Overdue - Tier {blocks}"
    return {"amount": amount, "due_date": due_date, "days_overdue": days, "grace_end": due_date + timedelta(days=grace),
            "blocks": blocks, "tier_bp": tier_bp, "tier_pct": tier_bp / 100, "fine": fine, "total": amount + fine,
            "capped": capped, "flag": flag, "next_increase": next_date, "mode": mode,
            "grace_days": grace, "period_days": period, "terms": t}


def loan_terms(group, loan) -> dict:
    """The late-fine terms this loan is charged under, ready to display.

    `older` is True when the group has changed its fine rules since this loan was created: the loan keeps the terms it was
    agreed on, and the group's newer terms apply only to loans created after the change."""
    cur = get_settings(group.id)
    t = fine_terms.terms(loan)
    cur_t = fine_terms.terms(cur)
    cc, dd = group.currency, group.currency_decimals
    changed_at = to_local(cur.fine_terms_since, group.timezone) if cur.fine_terms_since else None
    return {"terms": t, "text": fine_terms.describe(t, cc, dd), "headline": fine_terms.headline(t, cc, dd),
            "free": fine_terms.is_free(t), "older": not fine_terms.same(loan, cur),
            "current_text": fine_terms.describe(cur_t, cc, dd), "current_headline": fine_terms.headline(cur_t, cc, dd),
            "since": changed_at.date() if changed_at else None, "created_at": loan.created_at}


def scan(group, today) -> list[dict]:
    """Every instalment that is past its due date and not fully paid, with each loan's own terms applied. Longest overdue first."""
    q = (db.session.query(S, Loan, Borrower).join(Loan, Loan.id == S.loan_id).join(Borrower, Borrower.id == Loan.borrower_id)
         .filter(S.group_id == group.id, Loan.group_id == group.id, Loan.disbursement_tx_id.isnot(None),
                 Loan.status.in_(ACTIVE_STATES), S.due_date < today, (S.principal_due + S.interest_due) > (S.principal_paid + S.interest_paid)))
    cur = get_settings(group.id)
    rows = []
    for s, loan, b in q:
        a = assess(s.balance, s.due_date, today, terms=loan)
        a.update(sched=s, loan=loan, borrower=b, older_terms=not fine_terms.same(loan, cur))
        rows.append(a)
    rows.sort(key=lambda r: (-r["days_overdue"], r["loan"].reference, r["sched"].number))
    return rows


def statement(group, loan, today) -> dict | None:
    """Loan-level late-fine statement: every overdue instalment of one loan with the policy applied, plus the
    before / after picture the borrower needs to understand what they are being asked to pay and why.

    "Principal" in the headline figures follows the overdue log: it is what is still unpaid on the overdue
    instalments (principal and interest), i.e. the late amount the fine is charged on.

        principal_before  = unpaid amount on the overdue instalments, before any fine
        fine              = sum of the per-instalment fines (each worked out by assess())
        principal_after   = principal_before + fine
        balance_before    = everything still owed on the loan (principal + interest, overdue or not)
        balance_after     = balance_before + fine

    Every figure uses the LOAN's own frozen late-fine terms (`terms` in the result), never the group's current rules.

    Returns None when the loan has nothing overdue (not disbursed, finished, written off, or fully up to date).
    Informational only: nothing is posted to the ledger."""
    if loan.group_id != group.id or not loan.disbursement_tx_id or loan.status not in ACTIVE_STATES:
        return None
    lines = []
    for s in loan.schedule:
        if s.due_date >= today or s.balance <= 0:            # same test as scan(): past due and not fully paid
            continue
        a = assess(s.balance, s.due_date, today, terms=loan)
        # the working, before any cap: percent of the late amount, or the fixed fee, times the number of periods
        raw_pct = a["terms"].fine_rate_bp * a["blocks"] / 100 if a["terms"].fine_type == "PERCENT" else None
        a.update(number=s.number, principal_due=s.principal_due, interest_due=s.interest_due, paid=s.total_paid, raw_pct=raw_pct)
        lines.append(a)
    if not lines:
        return None
    before = sum(x["amount"] for x in lines)
    fine = sum(x["fine"] for x in lines)
    p_out, i_out = outstanding(loan)
    next_dates = [x["next_increase"] for x in lines if x["next_increase"]]
    return {
        "loan": loan, "borrower": loan.borrower, "date": today, "lines": lines, "mode": lines[0]["mode"],
        "principal_before": before, "fine": fine, "principal_after": before + fine,
        "balance_before": p_out + i_out, "balance_after": p_out + i_out + fine,
        "paid_to_date": sum(s.total_paid for s in loan.schedule),
        "longest_days": max(x["days_overdue"] for x in lines),
        "fined": sum(1 for x in lines if x["fine"] > 0), "capped": sum(1 for x in lines if x["capped"]),
        "next_increase": min(next_dates) if next_dates else None,
        "terms": lines[0]["terms"], "terms_info": loan_terms(group, loan),
    }


def trackers(group_id, schedule_ids) -> dict:
    if not schedule_ids:
        return {}
    return {t.schedule_id: t for t in LateFeeTracker.query.filter(
        LateFeeTracker.group_id == group_id, LateFeeTracker.schedule_id.in_(list(schedule_ids)))}


def alert_text(group, a) -> tuple[str, str]:
    """(title, body) of the administrator alert, in the agreed format."""
    m = lambda v: fmt(v, group.currency, group.currency_decimals)  # noqa: E731
    loan, b = a["loan"], a["borrower"]
    title = f"Late fee: {b.name} - {loan.reference} ({a['flag']})"
    body = (f"Borrower: {b.name} | Loan ID: {loan.reference}\n"
            f"Original due date: {a['due_date']:%d %b %Y} | Days overdue: {a['days_overdue']}\n"
            f"Original late amount: {m(a['amount'])}\n"
            f"Penalty tier: {a['tier_pct']:g}% | Accumulated fine: {m(a['fine'])}\n"
            f"New total balance due: {m(a['total'])}\n"
            f"Status: {a['flag']}")
    return title, body


def run_daily(group, today) -> int:
    """Alert the administrators once for every instalment that has entered a new penalty block. Safe to call often."""
    rows = [r for r in scan(group, today) if r["blocks"] > 0 and r["fine"] > 0]
    if not rows:
        return 0
    known = trackers(group.id, [r["sched"].id for r in rows])
    officials, sent = notify.official_ids(group.id), 0
    for a in rows:
        s, t = a["sched"], known.get(a["sched"].id)
        if t is None:
            t = LateFeeTracker(group_id=group.id, loan_id=a["loan"].id, schedule_id=s.id, last_block=0, last_fine=0)
            db.session.add(t)
        if a["blocks"] <= t.last_block:
            continue
        title, body = alert_text(group, a)
        notify.notify(officials, group.id, "LATE_FEE", title, body, f"/g/{group.id}/overdue/{s.id}/notice.pdf",
                      dedupe_key=f"latefee:{s.id}:{a['blocks']}")
        t.last_block, t.last_fine, t.last_notified_at = a["blocks"], a["fine"], utcnow()
        sent += 1
    db.session.flush()
    return sent


def run_daily_once(group, today) -> int:
    """run_daily, at most once per group per calendar day per process (called from dashboards)."""
    done = current_app.extensions.setdefault("late_fee_runs", {})
    if done.get(group.id) == today:
        return 0
    n = run_daily(group, today)
    done[group.id] = today
    return n


def log_rows(group, today) -> list[dict]:
    """The administrator tracking log: one row per overdue instalment, exact columns."""
    rows = scan(group, today)
    tr = trackers(group.id, [r["sched"].id for r in rows])
    out = []
    for a in rows:
        t = tr.get(a["sched"].id)
        a["last_notified"] = t.last_notified_at if t else None
        out.append(a)
    return out


LOG_COLUMNS = ["Loan ID", "Borrower Name", "Due Date", "Days Overdue", "Principal Due", "Penalty %", "Fine Accrued",
               "Total Owed", "Last Notified"]
