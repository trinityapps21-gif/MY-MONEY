"""Company late-fee policy: monitor overdue repayments, work out the fine, alert administrators.

POLICY
  * Grace period: no fine from the due date (day 0) through day 7 after it.
  * After the grace period: 5% of the original late amount for every 7-day block.
        days_overdue   = today - due_date
        billable_days  = days_overdue - 7
        blocks         = 0 if billable_days <= 0, else floor(billable_days / 7)      (mode COMPLETED, the default)
                                                      or ceil(billable_days / 7)       (mode STARTED)
        raw_fine       = late_amount * 0.05 * blocks
        final_fine     = min(raw_fine, late_amount)                    # never more than 100% of the late amount
        total_owed     = late_amount + final_fine
  * Whenever a borrower enters a new penalty block, every administrator gets one alert.

Set LATE_FEE_BLOCK_MODE=STARTED to charge at the START of each 7-day block (ceil) instead of when it is completed.
These figures are informational: this module never posts to the ledger. Collecting money still goes through the
normal repayment screens. The late amount is what is still unpaid on the instalment (principal + interest)."""
import math
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from flask import current_app, has_app_context

from app.extensions import db
from app.models import Borrower, Loan, LoanRepaymentSchedule as S, LateFeeTracker, utcnow
from app.services import notify
from app.services.loans import ACTIVE_STATES
from app.services.money import fmt

GRACE_DAYS = 7
BLOCK_DAYS = 7
RATE_BP_PER_BLOCK = 500          # 5%
CAP_BP = 10000                   # the fine can never exceed 100% of the original late amount


def block_mode() -> str:
    mode = (current_app.config.get("LATE_FEE_BLOCK_MODE") if has_app_context() else None) or "COMPLETED"
    mode = mode.upper()
    return mode if mode in ("COMPLETED", "STARTED") else "COMPLETED"


def assess(amount: int, due_date, today, mode: str | None = None) -> dict:
    """Apply the policy to one late amount (minor units). Pure function: no database access."""
    mode = mode or block_mode()
    days = (today - due_date).days
    billable = days - GRACE_DAYS
    if billable <= 0:
        blocks = 0
    elif mode == "STARTED":
        blocks = math.ceil(billable / BLOCK_DAYS)
    else:
        blocks = billable // BLOCK_DAYS
    raw = (Decimal(amount) * RATE_BP_PER_BLOCK * blocks / Decimal(10000)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    fine = int(min(raw, Decimal(amount)))
    capped = blocks > 0 and raw >= amount
    tier_bp = min(RATE_BP_PER_BLOCK * blocks, CAP_BP)
    # first day the NEXT block starts charging (None once the cap is reached)
    if capped:
        next_date = None
    elif mode == "STARTED":
        next_date = due_date + timedelta(days=GRACE_DAYS + BLOCK_DAYS * blocks + 1)
    else:
        next_date = due_date + timedelta(days=GRACE_DAYS + BLOCK_DAYS * (blocks + 1))
    if days <= 0:
        flag = "Not yet due"
    elif days <= GRACE_DAYS:
        flag = "Grace Period"
    elif blocks == 0:
        flag = "Active Overdue - No Fine Yet"      # grace is over, the first full 7-day block has not finished
    elif capped:
        flag = "Max Fine Cap Reached"
    else:
        flag = f"Active Overdue - Tier {blocks}"
    return {"amount": amount, "due_date": due_date, "days_overdue": days, "grace_end": due_date + timedelta(days=GRACE_DAYS),
            "blocks": blocks, "tier_bp": tier_bp, "tier_pct": tier_bp / 100, "fine": fine, "total": amount + fine,
            "capped": capped, "flag": flag, "next_increase": next_date, "mode": mode}


def scan(group, today) -> list[dict]:
    """Every instalment that is past its due date and not fully paid, with the policy applied. Longest overdue first."""
    q = (db.session.query(S, Loan, Borrower).join(Loan, Loan.id == S.loan_id).join(Borrower, Borrower.id == Loan.borrower_id)
         .filter(S.group_id == group.id, Loan.group_id == group.id, Loan.disbursement_tx_id.isnot(None),
                 Loan.status.in_(ACTIVE_STATES), S.due_date < today, (S.principal_due + S.interest_due) > (S.principal_paid + S.interest_paid)))
    rows = []
    for s, loan, b in q:
        a = assess(s.balance, s.due_date, today)
        a.update(sched=s, loan=loan, borrower=b)
        rows.append(a)
    rows.sort(key=lambda r: (-r["days_overdue"], r["loan"].reference, r["sched"].number))
    return rows


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
    rows = [r for r in scan(group, today) if r["blocks"] > 0]
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
