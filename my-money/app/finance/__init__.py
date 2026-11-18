from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import GroupMember
from app.services import schedules as sched_svc
from app.services.audit import audit
from app.services.groupcfg import get_settings
from app.services.tenant import get_scoped_or_404, tenant_route
from app.services.timeutil import local_today

bp = Blueprint("schedules", __name__, url_prefix="/g/<int:group_id>/schedule")


def _save(member):
    day = request.form.get("due_day", type=int)
    month = request.form.get("due_month", type=int)
    if day is None:
        raise ValueError("Choose your day.")
    sched_svc.set_schedule(g.group, member, day, month)


@bp.route("/me", methods=["GET", "POST"])
@tenant_route()
def mine(group_id=None):
    s = get_settings(g.group.id)
    if request.method == "POST":
        try:
            _save(g.membership)
            audit("schedule.set", "group_member", g.membership.id, group_id=g.group.id)
            db.session.commit()
            flash("Your contribution day is saved.", "success")
            return redirect(url_for("schedules.mine"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    row = sched_svc.get_schedule(g.group.id, g.membership.id)
    st = sched_svc.status_for(g.group, g.membership, row, local_today(g.group.timezone), s)
    return render_template("schedules/mine.html", s=s, row=row, st=st, weekdays=sched_svc.WEEKDAYS,
                           months=sched_svc.MONTHS, period_label=sched_svc.PERIOD_LABEL[s.contribution_period])


@bp.route("/")
@tenant_route(official=True)
def index(group_id=None):
    today = local_today(g.group.timezone)
    s = get_settings(g.group.id)
    rows = sched_svc.overview(g.group, today)
    counts = {k: sum(1 for r in rows if r["st"]["state"] == k) for k in
              ("OVERDUE", "DUE_TODAY", "DUE_SOON", "NO_DAY", "PAID", "PAID_AHEAD")}
    return render_template("schedules/index.html", rows=rows, counts=counts, s=s, weekdays=sched_svc.WEEKDAYS,
                           months=sched_svc.MONTHS, period_label=sched_svc.PERIOD_LABEL[s.contribution_period])


@bp.route("/<int:member_id>", methods=["POST"])
@tenant_route(official=True)
def set_for(member_id, group_id=None):
    m = get_scoped_or_404(GroupMember, member_id)
    try:
        _save(m)
        audit("schedule.set_by_official", "group_member", m.id, group_id=g.group.id)
        db.session.commit()
        flash("Contribution day saved.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("schedules.index"))
