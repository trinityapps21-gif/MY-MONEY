"""Financial Activity Feed: automatically generated timeline plus transparency
analytics. Members read it; nobody can create or edit these records by hand."""
from flask import Blueprint, g, render_template, request

from app.models import Announcement, GroupFeed, utcnow
from app.services import analytics, ledger
from app.services.tenant import scoped, tenant_route
from app.services.timeutil import local_today

bp = Blueprint("feed", __name__, url_prefix="/g/<int:group_id>/feed")


def _activity_query():
    q = scoped(GroupFeed)
    if not g.is_official:
        # Officials choose what ordinary members can see (Group settings).
        q = q.filter(GroupFeed.event_type.in_(sorted(g.group.visible_types) or ["__none__"]))
    return q.order_by(GroupFeed.created_at.desc(), GroupFeed.id.desc())


@bp.route("/")
@tenant_route()
def index(group_id=None):
    today = local_today(g.group.timezone)
    summary = analytics.group_summary(g.group, today)
    charts = analytics.dashboard_charts(g.group, today)
    board = analytics.leaderboard(g.group, today)
    page = _activity_query().paginate(page=1, per_page=10, error_out=False)
    pinned = scoped(Announcement, status="ACTIVE", is_pinned=True, is_published=True) \
        .filter(Announcement.publish_date <= utcnow()).order_by(Announcement.publish_date.desc()).all()
    chart_members = board[:8]
    return render_template("feed/index.html", s=summary, charts=charts, board=board, page=page, pinned=pinned,
                           chart_members=chart_members)


@bp.route("/activity")
@tenant_route()
def activity(group_id=None):
    """Lazy-loaded timeline pages (HTML fragment)."""
    page = _activity_query().paginate(page=request.args.get("page", 1, type=int), per_page=10, error_out=False)
    return render_template("feed/_activity.html", page=page)
