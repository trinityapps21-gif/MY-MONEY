"""Collections worklist and portfolio ageing (arrears) for lending businesses."""
from datetime import timedelta

from app.models import Loan
from app.services import loans as loan_svc

BUCKETS = (("Current", None), ("1-30 days", 30), ("31-60 days", 60), ("61-90 days", 90), ("Over 90 days", 10 ** 6))


def active_loans(group, officer_id=None):
    q = Loan.query.filter(Loan.group_id == group.id, Loan.status.in_(loan_svc.ACTIVE_STATES),
                          Loan.disbursement_tx_id.isnot(None))
    if officer_id:
        q = q.filter(Loan.officer_id == officer_id)
    return q.order_by(Loan.id).all()


def days_late(loan, today) -> int:
    late = [s.due_date for s in loan.schedule if s.balance > 0 and s.due_date < today]
    return (today - min(late)).days if late else 0


def bucket_of(days: int) -> str:
    if days <= 0:
        return BUCKETS[0][0]
    for name, top in BUCKETS[1:]:
        if days <= top:
            return name
    return BUCKETS[-1][0]


def _row(loan, today):
    late = [s for s in loan.schedule if s.balance > 0 and s.due_date < today]
    nxt = loan_svc.next_due(loan)
    last = loan.follow_ups[0] if loan.follow_ups else None
    promise = next((f for f in loan.follow_ups if f.promise_date), None)
    return {"loan": loan, "borrower": loan.borrower, "officer": loan.officer_id, "days": days_late(loan, today),
            "overdue_amount": sum(s.balance for s in late), "next_due": nxt, "last": last,
            "promise": promise, "broken": bool(promise and promise.promise_date < today and late)}


def worklist(group, today, officer_id=None, horizon=7):
    overdue, due_today, soon = [], [], []
    for ln in active_loans(group, officer_id):
        row = _row(ln, today)
        if row["days"] > 0:
            overdue.append(row)
        elif row["next_due"] is not None and row["next_due"].due_date == today:
            due_today.append(row)
        elif row["next_due"] is not None and today < row["next_due"].due_date <= today + timedelta(days=horizon):
            soon.append(row)
    overdue.sort(key=lambda r: -r["days"])
    soon.sort(key=lambda r: r["next_due"].due_date)
    return {"overdue": overdue, "due_today": due_today, "soon": soon}


def aging(group, today, officer_id=None):
    """Per-loan arrears rows plus bucket totals and portfolio-at-risk ratios (by outstanding principal)."""
    loans = active_loans(group, officer_id)
    totals = {name: [0, 0] for name, _ in BUCKETS}   # name -> [loan count, principal outstanding]
    rows, book = [], 0
    for ln in loans:
        p, _i = loan_svc.outstanding(ln)
        d = days_late(ln, today)
        b = bucket_of(d)
        totals[b][0] += 1
        totals[b][1] += p
        book += p
        if d > 0:
            late = [s for s in ln.schedule if s.balance > 0 and s.due_date < today]
            rows.append({"loan": ln, "days": d, "bucket": b, "overdue": sum(s.balance for s in late), "principal": p})
    rows.sort(key=lambda r: -r["days"])

    def par(days):
        return sum(r["principal"] for r in rows if r["days"] > days)

    return {"rows": rows, "totals": totals, "book": book,
            "par": {x: par(x) for x in (0, 30, 60, 90)},
            "par_pct": {x: (par(x) * 100.0 / book if book else 0.0) for x in (0, 30, 60, 90)}}
