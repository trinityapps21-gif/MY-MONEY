"""Group income and expenses (officials only): categories, itemised expenses,
totals and chart series. Everything is derived from the ledger so it always
agrees with the group's cash."""
from sqlalchemy import case, func

from app.extensions import db
from app.models import (Expense, ExpenseCategory, ExpenseItem, IncomeCategory, IncomeRecord, Transaction)
from app.services import ledger

AUTO_INCOME = [("Loan interest", "INTEREST_REPAYMENT", "*"), ("Fine interest", "PENALTY", "FINE"),
               ("Other penalties", "PENALTY", None)]
DEFAULT_INCOME = ["Registration fees", "Donations", "Membership fees", "Events"]
DEFAULT_EXPENSE = ["Annual meeting", "Stationery", "Transport", "Rent", "Airtime and data", "Other"]


class FinanceError(ValueError):
    pass


# ---------------------------------------------------------------- categories
def ensure_categories(group_id):
    if IncomeCategory.query.filter_by(group_id=group_id).count() == 0:
        for n in DEFAULT_INCOME:
            db.session.add(IncomeCategory(group_id=group_id, name=n))
    if ExpenseCategory.query.filter_by(group_id=group_id).count() == 0:
        for n in DEFAULT_EXPENSE:
            db.session.add(ExpenseCategory(group_id=group_id, name=n))
    db.session.flush()


def add_category(model, group_id, name):
    name = (name or "").strip()[:60]
    if len(name) < 2:
        raise FinanceError("Enter a category name.")
    row = model.query.filter(model.group_id == group_id, func.lower(model.name) == name.lower()).first()
    if row:
        return row
    row = model(group_id=group_id, name=name)
    db.session.add(row)
    db.session.flush()
    return row


# ------------------------------------------------------------------- income
def record_income(group, user, category_id, amount, income_date, description=None, method=None):
    cat = IncomeCategory.query.filter_by(id=category_id, group_id=group.id).first()
    if cat is None:
        raise FinanceError("Choose an income category.")
    if amount <= 0:
        raise FinanceError("Amount must be greater than zero.")
    tx = ledger.post(group.id, "ADJUSTMENT", amount, "IN", income_date, user.id,
                     f"Income - {cat.name}" + (f": {description}" if description else ""), method, None,
                     prefix="INC", kind="INCOME")
    rec = IncomeRecord(group_id=group.id, category_id=cat.id, amount=amount, income_date=income_date,
                       description=(description or "")[:300], tx_id=tx.id, recorded_by=user.id)
    db.session.add(rec)
    db.session.flush()
    return rec


def reverse_income(group, rec: IncomeRecord, user, reason, today):
    if rec.group_id != group.id:
        raise FinanceError("Income record not found.")
    ledger.reverse(rec.tx, user.id, reason, today)


# ----------------------------------------------------------------- expenses
def record_expense(group, user, title, category_name, expense_date, items, method=None):
    """items: [(name, amount_minor)]. The expense total is the sum of its lines."""
    items = [(n.strip()[:120], a) for n, a in items if n and n.strip() and a]
    if not items:
        raise FinanceError("Add at least one item with an amount.")
    if any(a <= 0 for _, a in items):
        raise FinanceError("Every item needs an amount above zero.")
    if not (title or "").strip():
        raise FinanceError("Give the expense a title.")
    total = sum(a for _, a in items)
    if total > ledger.cash_balance(group.id):
        raise FinanceError("The group does not have enough cash for this expense.")
    cat = add_category(ExpenseCategory, group.id, category_name or "Other")
    tx = ledger.post(group.id, "EXPENSE", total, "OUT", expense_date, user.id,
                     f"{title.strip()} ({cat.name})"[:300], method, None, prefix="EXP")
    exp = Expense(group_id=group.id, category=cat.name, description=title.strip()[:300], amount=total,
                  expense_date=expense_date, tx_id=tx.id, recorded_by=user.id)
    db.session.add(exp)
    db.session.flush()
    for n, a in items:
        db.session.add(ExpenseItem(group_id=group.id, expense_id=exp.id, name=n, amount=a))
    db.session.flush()
    return exp


def reverse_expense(group, exp: Expense, user, reason, today):
    if exp.group_id != group.id:
        raise FinanceError("Expense not found.")
    ledger.reverse(db.session.get(Transaction, exp.tx_id), user.id, reason, today)


# ------------------------------------------------------------------- series
def _month_keys(today, months):
    keys, y, m = [], today.year, today.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(keys))


def _signed(positive):
    return case((Transaction.direction == positive, Transaction.amount), else_=-Transaction.amount)


def _income_filter():
    """Ledger rows that count as group income."""
    return ((Transaction.tx_type == "INTEREST_REPAYMENT")
            | (Transaction.tx_type == "PENALTY")
            | ((Transaction.tx_type == "ADJUSTMENT") & (Transaction.kind == "INCOME")))


def monthly(group_id, today, months=6):
    """(labels, income[], expenses[], net[]) from the ledger."""
    keys = _month_keys(today, months)
    month = func.strftime("%Y-%m", Transaction.tx_date)
    inc = dict(db.session.query(month, func.coalesce(func.sum(_signed("IN")), 0)).filter(
        Transaction.group_id == group_id, _income_filter(), month >= keys[0]).group_by(month).all())
    exp = dict(db.session.query(month, func.coalesce(func.sum(_signed("OUT")), 0)).filter(
        Transaction.group_id == group_id, Transaction.tx_type == "EXPENSE", month >= keys[0]).group_by(month).all())
    income = [int(inc.get(k, 0)) for k in keys]
    expenses = [int(exp.get(k, 0)) for k in keys]
    return keys, income, expenses, [a - b for a, b in zip(income, expenses)]


def income_breakdown(group_id, start=None, end=None):
    """[(source, amount)] largest first: the group's own categories plus
    loan interest and fine interest that arrive automatically."""
    out = {}
    q = db.session.query(IncomeCategory.name, func.coalesce(func.sum(_signed("IN")), 0)).join(
        IncomeRecord, IncomeRecord.category_id == IncomeCategory.id).join(
        Transaction, Transaction.id == IncomeRecord.tx_id).filter(IncomeRecord.group_id == group_id)
    # include reversals of those rows too
    if start:
        q = q.filter(Transaction.tx_date >= start)
    if end:
        q = q.filter(Transaction.tx_date <= end)
    for name, amt in q.group_by(IncomeCategory.name).all():
        out[name] = out.get(name, 0) + int(amt)
    # reversals of manual income rows are separate ledger rows: subtract them
    rq = db.session.query(IncomeCategory.name, func.coalesce(func.sum(Transaction.amount), 0)).join(
        IncomeRecord, IncomeRecord.category_id == IncomeCategory.id).join(
        Transaction, Transaction.reverses_id == IncomeRecord.tx_id).filter(IncomeRecord.group_id == group_id)
    if start:
        rq = rq.filter(Transaction.tx_date >= start)
    if end:
        rq = rq.filter(Transaction.tx_date <= end)
    for name, amt in rq.group_by(IncomeCategory.name).all():
        out[name] = out.get(name, 0) - int(amt)
    t = ledger.totals(group_id, start, end)
    for label, val in (("Loan interest", t["interest_collected"]), ("Fine interest", t["fines_collected"]),
                       ("Other penalties", t["penalties"] - t["fines_collected"])):
        if val:
            out[label] = out.get(label, 0) + val
    return sorted(((k, v) for k, v in out.items() if v), key=lambda kv: -kv[1])


def expense_breakdown(group_id, start=None, end=None):
    """[(category, amount)] net of reversed expenses."""
    q = db.session.query(Expense.category, Expense.amount, Expense.tx_id, Expense.expense_date).filter(
        Expense.group_id == group_id)
    if start:
        q = q.filter(Expense.expense_date >= start)
    if end:
        q = q.filter(Expense.expense_date <= end)
    reversed_ids = {r[0] for r in db.session.query(Transaction.reverses_id).filter(
        Transaction.group_id == group_id, Transaction.reverses_id.isnot(None))}
    out = {}
    for cat, amt, txid, _d in q.all():
        if txid not in reversed_ids:
            out[cat] = out.get(cat, 0) + int(amt)
    return sorted(out.items(), key=lambda kv: -kv[1])


def summary(group_id, start=None, end=None):
    t = ledger.totals(group_id, start, end)
    inc = t["interest_collected"] + t["penalties"] + t["other_income"]
    return {"income": inc, "expenses": t["expenses"], "net": inc - t["expenses"],
            "loan_interest": t["interest_collected"], "fines": t["penalties"], "other_income": t["other_income"]}
