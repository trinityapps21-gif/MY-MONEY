"""Group-wide savings view: the group's total and what every member has contributed.
All figures come from the ledger (net of reversals), so they match the dashboard and reports."""
from sqlalchemy import case, func
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import GroupMember, Transaction
from app.services import ledger

SAVINGS_TYPES = ("CONTRIBUTION", "SAVINGS_ADDITIONAL")


def breakdown(group):
    """{'total', 'rows', 'other'}: the group's total savings and one row per member (largest first).

    'other' is any savings in the ledger that is not linked to a listed member, so the rows always add up to total."""
    total = ledger.totals(group.id)["total_savings"]
    signed = case((Transaction.direction == "IN", Transaction.amount), else_=-Transaction.amount)
    last_in = func.max(case((Transaction.direction == "IN", Transaction.tx_date)))
    sums = {mid: (int(s or 0), last) for mid, s, last in db.session.query(
        Transaction.member_id, func.coalesce(func.sum(signed), 0), last_in).filter(
        Transaction.group_id == group.id, Transaction.tx_type.in_(SAVINGS_TYPES),
        Transaction.member_id.isnot(None)).group_by(Transaction.member_id)}
    members = GroupMember.query.options(joinedload(GroupMember.user)).filter_by(group_id=group.id).all()
    rows = []
    for m in members:
        amount, last = sums.get(m.id, (0, None))
        if m.status != "ACTIVE" and amount == 0:
            continue                                  # people who left with nothing in the pot are not listed
        rows.append({"member": m, "name": m.user.full_name, "total": amount, "last": last,
                     "active": m.status == "ACTIVE", "share": (amount / total * 100) if total > 0 else 0.0})
    rows.sort(key=lambda r: (-r["total"], r["name"].lower()))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"total": total, "rows": rows, "other": total - sum(r["total"] for r in rows)}
