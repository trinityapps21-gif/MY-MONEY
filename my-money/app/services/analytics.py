"""Dashboard, feed and platform analytics. Every figure is computed from the
ledger / loan tables; nothing is hard-coded."""
from datetime import timedelta

from sqlalchemy import case, func

from app.extensions import db
from app.models import (Group, GroupMember, Loan, LoanRepaymentSchedule, Transaction, User, utcnow)
from app.services import ledger, loans as loan_svc

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def month_label(key: str) -> str:
    return MONTH_NAMES[int(key[5:7]) - 1]


def group_summary(group, today, start=None, end=None):
    """All dashboard cards in one dict. ``start``/``end`` window the flow
    figures (contributions, repayments...); balances are always current."""
    all_time = ledger.totals(group.id)
    window = ledger.totals(group.id, start, end) if (start or end) else all_time
    pf = loan_svc.portfolio(group, today)
    members = GroupMember.query.filter_by(group_id=group.id, status="ACTIVE").count()
    return {
        "total_savings": all_time["total_savings"],
        "available_cash": all_time["cash"],
        "principal_lent": pf["principal_disbursed"],
        "principal_outstanding": pf["principal_outstanding"],
        "interest_earned": pf["interest_earned"],
        "interest_collected": pf["interest_collected"],
        "interest_receivable": pf["interest_receivable"],
        "total_repayments": pf["total_repayments"],
        "active_members": members,
        "active_loans": pf["active_loans"],
        "overdue_loans": pf["overdue_loans"],
        "fully_repaid_loans": pf["fully_repaid_loans"],
        "loan_count": pf["loan_count"],
        "active_borrowers": pf["active_borrowers"],
        "period_savings": window["total_savings"],
        "period_repayments": window["total_repayments"],
        "period_interest": window["interest_collected"],
        "period_disbursed": window["disbursed"],
        "expenses": all_time["expenses"],
    }


def _series(group_id, types, months, today):
    rows = ledger.monthly_net(group_id, types, months, today)
    return [month_label(k) for k, _ in rows], [v for _, v in rows]


def dashboard_charts(group, today, months=6):
    labels, savings = _series(group.id, ["CONTRIBUTION", "SAVINGS_ADDITIONAL"], months, today)
    _, repay = _series(group.id, ["PRINCIPAL_REPAYMENT", "INTEREST_REPAYMENT"], months, today)
    _, disb = _series(group.id, ["LOAN_DISBURSEMENT"], months, today)
    _, interest = _series(group.id, ["INTEREST_REPAYMENT"], months, today)
    _, prin_repaid = _series(group.id, ["PRINCIPAL_REPAYMENT"], months, today)

    # Cumulative trend: running savings and running outstanding principal at each month end.
    keys = [k for k, _ in ledger.monthly_net(group.id, ["CONTRIBUTION"], months, today)]
    before_start = f"{keys[0]}-01"
    base = ledger.totals(group.id, None, _prev_day(before_start))
    cum_sav, cum_out = [], []
    run_s = base["total_savings"]
    run_o = base["disbursed"] - base["principal_repaid"]
    for i in range(months):
        run_s += savings[i]
        run_o += disb[i] - prin_repaid[i]
        cum_sav.append(run_s)
        cum_out.append(run_o)
    return {
        "labels": labels, "savings": savings, "repayments": repay, "disbursements": disb,
        "interest": interest, "cumulative_savings": cum_sav, "outstanding_trend": cum_out,
    }


def _prev_day(iso):
    from datetime import date
    return date.fromisoformat(iso) - timedelta(days=1)


def leaderboard(group, today):
    """Per-member contribution totals (net of reversals), ranked."""
    signed = case((Transaction.direction == "IN", Transaction.amount), else_=-Transaction.amount)
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    q = db.session.query(
        GroupMember.id, User.full_name,
        func.coalesce(func.sum(signed), 0),
        func.coalesce(func.sum(case((Transaction.tx_date >= month_start, signed), else_=0)), 0),
        func.coalesce(func.sum(case((Transaction.tx_date >= year_start, signed), else_=0)), 0),
    ).select_from(GroupMember).join(User, User.id == GroupMember.user_id) \
        .outerjoin(Transaction, (Transaction.member_id == GroupMember.id) & (Transaction.group_id == group.id)
                   & Transaction.tx_type.in_(["CONTRIBUTION", "SAVINGS_ADDITIONAL"])) \
        .filter(GroupMember.group_id == group.id, GroupMember.status == "ACTIVE") \
        .group_by(GroupMember.id, User.full_name)
    rows = [{"member_id": r[0], "name": r[1], "total": int(r[2]), "month": int(r[3]), "year": int(r[4])} for r in q]
    rows.sort(key=lambda r: (-r["total"], r["name"].lower()))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def members_contributed(group, start, end):
    """(members with a net-positive contribution in the window, active members)."""
    signed = case((Transaction.direction == "IN", Transaction.amount), else_=-Transaction.amount)
    q = db.session.query(Transaction.member_id, func.sum(signed)).filter(
        Transaction.group_id == group.id, Transaction.tx_type.in_(["CONTRIBUTION", "SAVINGS_ADDITIONAL"]),
        Transaction.member_id.isnot(None))
    if start:
        q = q.filter(Transaction.tx_date >= start)
    if end:
        q = q.filter(Transaction.tx_date <= end)
    contributed = sum(1 for _, s in q.group_by(Transaction.member_id) if (s or 0) > 0)
    total = GroupMember.query.filter_by(group_id=group.id, status="ACTIVE").count()
    return contributed, total


def financial_mix(group):
    """Donut for Reports: share of savings / loans lent / interest collected."""
    t = ledger.totals(group.id)
    parts = {"Savings": max(t["total_savings"], 0), "Loans": max(t["disbursed"], 0),
             "Interest": max(t["interest_collected"], 0)}
    total = sum(parts.values())
    pct = {k: (round(v * 100 / total) if total else 0) for k, v in parts.items()}
    return {"labels": list(parts), "values": list(parts.values()), "percent": pct}


# ------------------------------------------------------------------ platform
def platform_summary():
    groups = db.session.query(func.count(Group.id)).scalar()
    active = db.session.query(func.count(Group.id)).filter(Group.status == "ACTIVE").scalar()
    users = db.session.query(func.count(User.id)).filter(User.is_platform_admin.is_(False)).scalar()
    members = db.session.query(func.count(GroupMember.id)).filter(GroupMember.status == "ACTIVE").scalar()
    sums = dict(db.session.query(
        Transaction.tx_type,
        func.coalesce(func.sum(case((Transaction.direction == "IN", Transaction.amount), else_=-Transaction.amount)), 0),
    ).group_by(Transaction.tx_type))
    loans_issued = db.session.query(func.count(Loan.id)).filter(Loan.disbursement_tx_id.isnot(None)).scalar()
    return {
        "total_groups": groups, "active_groups": active, "suspended_groups": groups - active,
        "total_users": users, "total_members": members,
        "total_contributions": int(sums.get("CONTRIBUTION", 0)) + int(sums.get("SAVINGS_ADDITIONAL", 0)),
        "loans_issued": loans_issued,
        "principal_disbursed": -int(sums.get("LOAN_DISBURSEMENT", 0)),
        "total_repayments": int(sums.get("PRINCIPAL_REPAYMENT", 0)) + int(sums.get("INTEREST_REPAYMENT", 0)),
        "principal_repaid": int(sums.get("PRINCIPAL_REPAYMENT", 0)),
    }


def platform_outstanding():
    S = LoanRepaymentSchedule
    total = db.session.query(func.coalesce(func.sum(S.principal_due - S.principal_paid), 0)) \
        .join(Loan, Loan.id == S.loan_id) \
        .filter(Loan.status.in_(loan_svc.ACTIVE_STATES), Loan.disbursement_tx_id.isnot(None)).scalar()
    return int(total or 0)


def platform_growth(months=6):
    """Cumulative groups and users per month for the platform growth chart."""
    today = utcnow().date()
    keys, y, m = [], today.year, today.month
    for _ in range(months):
        keys.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    labels, groups, users = [], [], []
    for y, m in keys:
        nm, ny = (1, y + 1) if m == 12 else (m + 1, y)
        from datetime import datetime
        cutoff = datetime(ny, nm, 1)
        groups.append(db.session.query(func.count(Group.id)).filter(Group.created_at < cutoff).scalar())
        users.append(db.session.query(func.count(User.id)).filter(User.created_at < cutoff,
                                                                  User.is_platform_admin.is_(False)).scalar())
        labels.append(MONTH_NAMES[m - 1])
    return {"labels": labels, "groups": groups, "users": users}
