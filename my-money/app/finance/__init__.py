"""Group income and expenses. Visible to the three officials only."""
from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import Expense, ExpenseCategory, ExpenseItem, IncomeCategory, IncomeRecord, Transaction
from app.services import finance as fin
from app.services.audit import audit
from app.services.forms import form_amount, form_date, form_text
from app.services.money import to_minor
from app.services.savings import PAYMENT_METHODS
from app.services.tenant import get_scoped_or_404, tenant_route
from app.services.timeutil import local_today

bp = Blueprint("finance", __name__, url_prefix="/g/<int:group_id>/finance")


@bp.route("/")
@tenant_route(official=True)
def index(group_id=None):
    today = local_today(g.group.timezone)
    year_start = date(today.year, 1, 1)
    month_start = date(today.year, today.month, 1)
    labels, income, expenses, net = fin.monthly(g.group.id, today, 6)
    inc_cat = fin.income_breakdown(g.group.id, year_start, today)
    exp_cat = fin.expense_breakdown(g.group.id, year_start, today)
    return render_template("finance/index.html", year=fin.summary(g.group.id, year_start, today),
                           month=fin.summary(g.group.id, month_start, today), labels=labels, income=income,
                           expenses=expenses, net=net, inc_cat=inc_cat, exp_cat=exp_cat, today=today)


# ------------------------------------------------------------------ income
@bp.route("/income")
@tenant_route(official=True)
def income(group_id=None):
    fin.ensure_categories(g.group.id)
    db.session.commit()
    cat = request.args.get("category", type=int)
    q = IncomeRecord.query.filter_by(group_id=g.group.id)
    if cat:
        q = q.filter(IncomeRecord.category_id == cat)
    page = q.order_by(IncomeRecord.income_date.desc(), IncomeRecord.id.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=15, error_out=False)
    reversed_ids = {r[0] for r in db.session.query(Transaction.reverses_id).filter(
        Transaction.group_id == g.group.id, Transaction.reverses_id.isnot(None))}
    today = local_today(g.group.timezone)
    return render_template("finance/income.html", page=page, cats=IncomeCategory.query.filter_by(
        group_id=g.group.id).order_by(IncomeCategory.name).all(), cat=cat, reversed_ids=reversed_ids,
        methods=PAYMENT_METHODS, today=today, breakdown=fin.income_breakdown(g.group.id, date(today.year, 1, 1), today))


@bp.route("/income/new", methods=["POST"])
@tenant_route(official=True)
def income_new():
    try:
        cat_id = request.form.get("category_id", type=int)
        new_name = (request.form.get("new_category") or "").strip()
        if new_name:
            cat_id = fin.add_category(IncomeCategory, g.group.id, new_name).id
        rec = fin.record_income(g.group, current_user, cat_id, form_amount(), form_date("income_date"),
                                form_text("description"), form_text("payment_method", 40))
        audit("income.recorded", "income_record", rec.id, {"amount": rec.amount}, group_id=g.group.id)
        db.session.commit()
        flash("Income recorded.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("finance.income"))


@bp.route("/income/<int:rec_id>/reverse", methods=["POST"])
@tenant_route(official=True)
def income_reverse(rec_id, group_id=None):
    rec = get_scoped_or_404(IncomeRecord, rec_id)
    try:
        fin.reverse_income(g.group, rec, current_user, request.form.get("reason") or "", local_today(g.group.timezone))
        audit("income.reversed", "income_record", rec.id, group_id=g.group.id)
        db.session.commit()
        flash("Income reversed.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("finance.income"))


# ---------------------------------------------------------------- expenses
@bp.route("/expenses")
@tenant_route(official=True)
def expenses(group_id=None):
    fin.ensure_categories(g.group.id)
    db.session.commit()
    cat = request.args.get("category")
    q = Expense.query.filter_by(group_id=g.group.id)
    if cat:
        q = q.filter(Expense.category == cat)
    page = q.order_by(Expense.expense_date.desc(), Expense.id.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=15, error_out=False)
    reversed_ids = {r[0] for r in db.session.query(Transaction.reverses_id).filter(
        Transaction.group_id == g.group.id, Transaction.reverses_id.isnot(None))}
    today = local_today(g.group.timezone)
    return render_template("finance/expenses.html", page=page, cat=cat, reversed_ids=reversed_ids,
                           cats=ExpenseCategory.query.filter_by(group_id=g.group.id).order_by(ExpenseCategory.name).all(),
                           breakdown=fin.expense_breakdown(g.group.id, date(today.year, 1, 1), today))


@bp.route("/expenses/new", methods=["GET", "POST"])
@tenant_route(official=True)
def expense_new(group_id=None):
    fin.ensure_categories(g.group.id)
    db.session.commit()
    if request.method == "POST":
        try:
            names = request.form.getlist("item_name")
            amounts = request.form.getlist("item_amount")
            items = []
            for n, a in zip(names, amounts):
                if (n or "").strip() or (a or "").strip():
                    if not (n or "").strip() or not (a or "").strip():
                        raise ValueError("Each item needs both a name and an amount.")
                    items.append((n, to_minor(a, g.group.currency_decimals)))
            cat = (request.form.get("new_category") or "").strip() or request.form.get("category") or "Other"
            exp = fin.record_expense(g.group, current_user, request.form.get("title"), cat,
                                     form_date("expense_date"), items, form_text("payment_method", 40))
            audit("expense.recorded", "expense", exp.id, {"amount": exp.amount}, group_id=g.group.id)
            db.session.commit()
            flash("Expense recorded.", "success")
            return redirect(url_for("finance.expense_detail", exp_id=exp.id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("finance/expense_new.html", cats=ExpenseCategory.query.filter_by(
        group_id=g.group.id).order_by(ExpenseCategory.name).all(), methods=PAYMENT_METHODS,
        today=local_today(g.group.timezone))


@bp.route("/expenses/<int:exp_id>")
@tenant_route(official=True)
def expense_detail(exp_id, group_id=None):
    exp = get_scoped_or_404(Expense, exp_id)
    items = ExpenseItem.query.filter_by(expense_id=exp.id).order_by(ExpenseItem.id).all()
    return render_template("finance/expense_detail.html", exp=exp, items=items,
                           is_reversed=Transaction.query.filter_by(reverses_id=exp.tx_id).first() is not None)


@bp.route("/expenses/<int:exp_id>/reverse", methods=["POST"])
@tenant_route(official=True)
def expense_reverse(exp_id, group_id=None):
    exp = get_scoped_or_404(Expense, exp_id)
    try:
        fin.reverse_expense(g.group, exp, current_user, request.form.get("reason") or "", local_today(g.group.timezone))
        audit("expense.reversed", "expense", exp.id, group_id=g.group.id)
        db.session.commit()
        flash("Expense reversed.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("finance.expense_detail", exp_id=exp.id))
