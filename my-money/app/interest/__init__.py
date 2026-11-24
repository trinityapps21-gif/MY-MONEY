"""Interest page: received, expected and incoming interest, with the dates it falls due (group officials only)."""
from flask import Blueprint, g, render_template, request
from sqlalchemy.orm import selectinload

from app.models import Borrower, Loan
from app.services import interest_report as ir
from app.services.loans import ACTIVE_STATES
from app.services.tenant import tenant_route
from app.services.timeutil import local_today

bp = Blueprint("interest", __name__, url_prefix="/g/<int:group_id>/interest")


@bp.route("/")
@tenant_route(official=True)
def index(group_id=None):
    today = local_today(g.group.timezone)
    view = request.args.get("view", "upcoming")
    if view not in ir.VIEWS:
        view = "upcoming"
    page_no = request.args.get("page", 1, type=int)
    ov = ir.overview(g.group, today)
    by_month = ir.monthly_received(g.group.id, today, 6)
    page = rows = None
    if view == "loans":
        q = (Loan.query.join(Borrower, Borrower.id == Loan.borrower_id)
             .filter(Loan.group_id == g.group.id, Loan.disbursement_tx_id.isnot(None))
             .options(selectinload(Loan.schedule), selectinload(Loan.borrower))
             .order_by(Loan.status.in_(ACTIVE_STATES).desc(), Loan.created_at.desc(), Loan.id.desc()))
        term = (request.args.get("q") or "").strip()
        if term:
            q = q.filter(Borrower.name.ilike(f"%{term}%") | Loan.reference.ilike(f"%{term}%"))
        page = q.paginate(page=page_no, per_page=20, error_out=False)
        rows = ir.loan_rows(page.items, today)
    else:
        page = ir.date_rows_query(g.group.id, view, today).paginate(page=page_no, per_page=25, error_out=False)
        rows = [{"s": s, "loan": l, "borrower": b, "state": ir.row_state(s, l, today)} for s, l, b in page.items]
    return render_template("interest/index.html", ov=ov, view=view, views=ir.VIEWS, page=page, rows=rows, today=today,
                           q=(request.args.get("q") or "").strip(), by_month=by_month)
