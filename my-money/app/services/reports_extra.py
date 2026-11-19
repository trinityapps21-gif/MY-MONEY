"""Extra reports: shares, withdrawals, loan fines, income statement and itemised
expenses. Registered into the main report registry (officials only)."""
from app.extensions import db
from app.models import (Expense, ExpenseItem, GroupMember, LoanFine, SavingsWithdrawal, Transaction, User)
from app.services import finance as fin, ledger, shares as share_svc
from app.services.groupcfg import get_settings
from app.services.reports import Report, _clip, _range_label

TITLES = {
    "shares_register": "Shares and Ranks Report",
    "savings_withdrawals": "Savings Withdrawals Report",
    "loan_fines": "Loan Fines Report",
    "income_statement": "Income and Expenses Report",
    "expense_items": "Itemised Expenses Report",
}
HELP = {
    "shares_register": "Every member's shares, value and rank.",
    "savings_withdrawals": "Savings withdrawals requested and approved.",
    "loan_fines": "Late-payment fines on external loans and their status.",
    "income_statement": "All group income by category, expenses by category, and the net result.",
    "expense_items": "Every expense with its itemised lines.",
}
NO_DATES = ("shares_register",)


def _shares(group, today, start, end, **_):
    held = share_svc.holdings(group.id)
    ranks = share_svc.ensure_ranks(group.id)
    price = get_settings(group.id).share_price
    members = GroupMember.query.join(User, User.id == GroupMember.user_id).filter(
        GroupMember.group_id == group.id, GroupMember.status == "ACTIVE").order_by(User.full_name).all()
    rows, total = [], 0
    for m in sorted(members, key=lambda x: -held.get(x.id, 0)):
        u = held.get(m.id, 0)
        cur, _n, val = share_svc.rank_for(group.id, u, ranks)
        total += u
        rows.append([m.user.full_name, cur.name if cur else "No rank", u, val])
    return Report("shares_register", TITLES["shares_register"], f"As at {today:%d %b %Y}",
                  [("Member", "text"), ("Rank", "text"), ("Shares", "int"), ("Value", "money")], rows,
                  [("Total shares", total, "int"), ("Total value", total * price, "money"),
                   ("Price per share", price, "money")])


def _withdrawals(group, today, start, end, **_):
    q = SavingsWithdrawal.query.filter_by(group_id=group.id)
    q = _clip(q, db.func.date(SavingsWithdrawal.created_at), start, end)
    rows, total = [], 0
    for w in q.order_by(SavingsWithdrawal.created_at):
        rows.append([w.created_at.date(), w.member.user.full_name, w.amount, w.status.title(), w.receipt_no or ""])
        if w.status == "APPROVED":
            total += w.amount
    return Report("savings_withdrawals", TITLES["savings_withdrawals"], _range_label(start, end),
                  [("Date", "date"), ("Member", "text"), ("Amount", "money"), ("Status", "text"), ("Receipt", "text")],
                  rows, [("Approved withdrawals", total, "money")])


def _fines(group, today, start, end, **_):
    q = LoanFine.query.filter_by(group_id=group.id)
    q = _clip(q, LoanFine.paid_on, start, end)
    rows, t = [], {"UNPAID": 0, "PAID": 0, "WAIVED": 0}
    for f in q.order_by(LoanFine.paid_on, LoanFine.id):
        rows.append([f.paid_on, f.loan.reference, f.borrower.name, f.days_late, f.amount, f.status.title()])
        if f.status in t:
            t[f.status] += f.amount
    return Report("loan_fines", TITLES["loan_fines"], _range_label(start, end),
                  [("Date", "date"), ("Loan", "text"), ("Borrower", "text"), ("Days late", "int"), ("Fine", "money"),
                   ("Status", "text")], rows,
                  [("Waiting to be collected", t["UNPAID"], "money"), ("Collected", t["PAID"], "money"),
                   ("Waived", t["WAIVED"], "money")])


def _income_statement(group, today, start, end, **_):
    inc = fin.income_breakdown(group.id, start, end)
    exp = fin.expense_breakdown(group.id, start, end)
    ti, te = sum(v for _, v in inc), sum(v for _, v in exp)
    rows = [["Income", n, v] for n, v in inc] + [["Expense", n, v] for n, v in exp]
    return Report("income_statement", TITLES["income_statement"], _range_label(start, end),
                  [("Type", "text"), ("Category", "text"), ("Amount", "money")], rows,
                  [("Total income", ti, "money"), ("Total expenses", te, "money"), ("Net result", ti - te, "money")])


def _expense_items(group, today, start, end, **_):
    q = Expense.query.filter_by(group_id=group.id)
    q = _clip(q, Expense.expense_date, start, end)
    reversed_ids = {r[0] for r in db.session.query(Transaction.reverses_id).filter(
        Transaction.group_id == group.id, Transaction.reverses_id.isnot(None))}
    rows, total = [], 0
    for e in q.order_by(Expense.expense_date, Expense.id):
        if e.tx_id in reversed_ids:
            continue
        items = ExpenseItem.query.filter_by(expense_id=e.id).order_by(ExpenseItem.id).all()
        for i in items or []:
            rows.append([e.expense_date, e.description or e.category, e.category, i.name, i.amount])
        if not items:
            rows.append([e.expense_date, e.description or e.category, e.category, "-", e.amount])
        total += e.amount
    return Report("expense_items", TITLES["expense_items"], _range_label(start, end),
                  [("Date", "date"), ("Expense", "text"), ("Category", "text"), ("Item", "text"), ("Amount", "money")],
                  rows, [("Total expenses", total, "money")])


BUILDERS = {"shares_register": _shares, "savings_withdrawals": _withdrawals, "loan_fines": _fines,
            "income_statement": _income_statement, "expense_items": _expense_items}
