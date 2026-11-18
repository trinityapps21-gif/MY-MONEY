"""Append-only transaction ledger. The single source of truth for cash and
for every dashboard figure derived from money movements.

Cash effect: IN rows add, OUT rows subtract. Reversal rows are ordinary rows
with the opposite direction and the same ``tx_type``, so per-type nets stay
correct without ever editing or deleting history.
"""
import secrets
from app.models import utcnow

from sqlalchemy import case, func

from app.extensions import db
from app.models import Transaction
from app.models.finance import TX_IN_TYPES, TX_OUT_TYPES


class LedgerError(ValueError):
    pass


def new_ref(prefix="TXN") -> str:
    return f"{prefix}-{utcnow():%y%m%d}-{secrets.token_hex(3).upper()}"


def post(group_id, tx_type, amount, direction, tx_date, user_id, description=None, payment_method=None,
         payment_reference=None, member_id=None, borrower_id=None, loan_id=None, prefix="TXN", kind=None):
    """Add a ledger row to the session (caller commits => atomic with the
    business record that caused it)."""
    if amount <= 0:
        raise LedgerError("Amount must be greater than zero")
    tx = Transaction(
        group_id=group_id, reference=new_ref(prefix), tx_type=tx_type, direction=direction, amount=amount,
        tx_date=tx_date, description=(description or "")[:300], payment_method=payment_method,
        payment_reference=payment_reference, member_id=member_id, borrower_id=borrower_id, loan_id=loan_id,
        recorded_by=user_id, kind=kind,
    )
    db.session.add(tx)
    db.session.flush()
    return tx


def reverse(tx: Transaction, user_id, reason, tx_date) -> Transaction:
    """Post a reversal for ``tx``. Never mutates the original row."""
    if tx.is_reversal:
        raise LedgerError("A reversal cannot be reversed")
    if Transaction.query.filter_by(reverses_id=tx.id).first():
        raise LedgerError("This transaction has already been reversed")
    if not (reason or "").strip():
        raise LedgerError("A reason is required to reverse a transaction")
    rev = Transaction(
        group_id=tx.group_id, reference=new_ref("REV"), tx_type=tx.tx_type,
        direction="OUT" if tx.direction == "IN" else "IN", amount=tx.amount, tx_date=tx_date,
        description=f"Reversal of {tx.reference}: {reason.strip()}"[:300], member_id=tx.member_id,
        borrower_id=tx.borrower_id, loan_id=tx.loan_id, is_reversal=True, reverses_id=tx.id,
        reversal_reason=reason.strip()[:300], recorded_by=user_id, kind=tx.kind,
    )
    db.session.add(rev)
    db.session.flush()
    return rev


def is_reversed(tx_id) -> bool:
    return db.session.query(Transaction.id).filter_by(reverses_id=tx_id).first() is not None


def totals(group_id, start=None, end=None) -> dict:
    """Net figures from the ledger for an optional date window."""
    q = db.session.query(Transaction.tx_type, Transaction.kind, Transaction.direction,
                         func.coalesce(func.sum(Transaction.amount), 0)).filter(Transaction.group_id == group_id)
    if start:
        q = q.filter(Transaction.tx_date >= start)
    if end:
        q = q.filter(Transaction.tx_date <= end)
    raw = {(t, k, d): int(s) for t, k, d, s in q.group_by(Transaction.tx_type, Transaction.kind, Transaction.direction)}

    def _sum(t, direction, kind="*"):
        return sum(v for (tt, kk, dd), v in raw.items()
                   if tt == t and dd == direction and (kind == "*" or kk == kind))

    def net_in(t, kind="*"):
        return _sum(t, "IN", kind) - _sum(t, "OUT", kind)

    def net_out(t, kind="*"):
        return _sum(t, "OUT", kind) - _sum(t, "IN", kind)

    cash_in = sum(v for (_, _, d), v in raw.items() if d == "IN")
    cash_out = sum(v for (_, _, d), v in raw.items() if d == "OUT")
    contributions = net_in("CONTRIBUTION")
    additional = net_in("SAVINGS_ADDITIONAL")  # includes withdrawals (OUT rows)
    withdrawals = net_out("SAVINGS_ADDITIONAL", "SAVINGS_WITHDRAWAL")
    return {
        "contributions": contributions,
        "additional_savings": additional,
        "total_savings": contributions + additional,
        "withdrawals": withdrawals,
        "disbursed": net_out("LOAN_DISBURSEMENT"),
        "principal_repaid": net_in("PRINCIPAL_REPAYMENT"),
        "interest_collected": net_in("INTEREST_REPAYMENT"),
        "penalties": net_in("PENALTY"),
        "fines_collected": net_in("PENALTY", "FINE"),
        "expenses": net_out("EXPENSE"),
        "share_capital": net_in("ADJUSTMENT", "SHARE_PURCHASE"),
        "other_income": net_in("ADJUSTMENT", "INCOME"),
        "adjustments": net_in("ADJUSTMENT", None),
        "total_repayments": net_in("PRINCIPAL_REPAYMENT") + net_in("INTEREST_REPAYMENT"),
        "cash": cash_in - cash_out,
    }


def cash_balance(group_id) -> int:
    return totals(group_id)["cash"]


def monthly_net(group_id, tx_types, months, today):
    """[(YYYY-MM, net_amount)] for the trailing ``months`` calendar months."""
    types = list(tx_types)
    positive = "IN" if types[0] in TX_IN_TYPES else "OUT"
    signed = case((Transaction.direction == positive, Transaction.amount), else_=-Transaction.amount)
    y, m = today.year, today.month
    keys = []
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    month = func.strftime("%Y-%m", Transaction.tx_date)
    rows = db.session.query(month, func.coalesce(func.sum(signed), 0)) \
        .filter(Transaction.group_id == group_id, Transaction.tx_type.in_(types), month >= keys[0]) \
        .group_by(month).all()
    data = {k: int(v) for k, v in rows}
    return [(k, data.get(k, 0)) for k in keys]


def member_savings(group_id, member_id) -> int:
    """A member's total savings (contributions + voluntary), net of reversals."""
    signed = case((Transaction.direction == "IN", Transaction.amount), else_=-Transaction.amount)
    total = db.session.query(func.coalesce(func.sum(signed), 0)).filter(
        Transaction.group_id == group_id, Transaction.member_id == member_id,
        Transaction.tx_type.in_(["CONTRIBUTION", "SAVINGS_ADDITIONAL"])).scalar()
    return int(total or 0)
