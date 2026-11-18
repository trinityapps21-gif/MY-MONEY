"""Member contribution schedules, countdowns and reminders.

The group's officials choose the frequency (weekly / monthly / annual); each
member chooses their own day. No fines are ever charged to members - they get a
reminder ``reminder_days`` before the due date and a countdown on their dashboard."""
import calendar
from datetime import date, timedelta

from app.extensions import db
from app.models import Contribution, GroupMember, MemberSchedule, Transaction, User, utcnow
from app.services import notify
from app.services.groupcfg import get_settings

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
PERIOD_LABEL = {"WEEKLY": "weekly", "MONTHLY": "monthly", "ANNUAL": "annually"}


def _clamp(y, m, d):
    return date(y, m, min(d, calendar.monthrange(y, m)[1]))


def _add_months(y, m, n):
    t = y * 12 + (m - 1) + n
    return t // 12, t % 12 + 1


def due_dates(period, day, month, today):
    """(previous_due, last_due, next_due): last_due <= today < next_due."""
    if period == "WEEKLY":
        last = today - timedelta(days=(today.weekday() - day) % 7)
        return last - timedelta(days=7), last, last + timedelta(days=7)
    if period == "MONTHLY":
        cand = _clamp(today.year, today.month, day)
        y, m = (today.year, today.month) if cand <= today else _add_months(today.year, today.month, -1)
        last = _clamp(y, m, day)
        py, pm = _add_months(y, m, -1)
        ny, nm = _add_months(y, m, 1)
        return _clamp(py, pm, day), last, _clamp(ny, nm, day)
    month = month or 1
    cand = _clamp(today.year, month, day)
    y = today.year if cand <= today else today.year - 1
    return _clamp(y - 1, month, day), _clamp(y, month, day), _clamp(y + 1, month, day)


def describe(period, day, month):
    if day is None:
        return "No day chosen"
    if period == "WEEKLY":
        return f"Every {WEEKDAYS[day % 7]}"
    if period == "MONTHLY":
        return f"Day {day} of every month"
    return f"Every {day} {MONTHS[(month or 1) - 1]}"


def get_schedule(group_id, member_id):
    return MemberSchedule.query.filter_by(group_id=group_id, member_id=member_id).first()


def set_schedule(group, member, day, month=None):
    s = get_settings(group.id)
    if s.contribution_period == "WEEKLY":
        if not 0 <= day <= 6:
            raise ValueError("Choose a day of the week.")
        month = None
    elif s.contribution_period == "MONTHLY":
        if not 1 <= day <= 28:
            raise ValueError("Choose a day between 1 and 28.")
        month = None
    else:
        if not 1 <= day <= 28 or not month or not 1 <= month <= 12:
            raise ValueError("Choose a month and a day between 1 and 28.")
    row = get_schedule(group.id, member.id)
    if row is None:
        row = MemberSchedule(group_id=group.id, member_id=member.id)
        db.session.add(row)
    row.due_day, row.due_month, row.updated_at = day, month, utcnow()
    return row


def _reversed_ids(group_id):
    return {r[0] for r in db.session.query(Transaction.reverses_id).filter(
        Transaction.group_id == group_id, Transaction.reverses_id.isnot(None))}


def _payments_after(group_id, member_id, after, reversed_ids=None):
    if reversed_ids is None:
        reversed_ids = _reversed_ids(group_id)
    rows = Contribution.query.filter(
        Contribution.group_id == group_id, Contribution.member_id == member_id,
        Contribution.category != "VOLUNTARY", Contribution.contribution_date > after).all()
    return sum(1 for c in rows if c.tx_id not in reversed_ids)


def status_for(group, member, row, today, settings=None, reversed_ids=None):
    """The member's countdown state. ``row`` may be None (no day chosen yet)."""
    s = settings or get_settings(group.id)
    if row is None:
        return {"state": "NO_DAY", "label": "Choose your contribution day", "next_due": None, "days": None,
                "overdue_days": 0, "last_due": None}
    prev, last, nxt = due_dates(s.contribution_period, row.due_day, row.due_month, today)
    joined = (member.joined_at or member.created_at).date()
    n = _payments_after(group.id, member.id, prev, reversed_ids)
    days_next = (nxt - today).days
    base = {"next_due": nxt, "days": days_next, "last_due": last, "overdue_days": 0}
    if n == 0 and last >= joined:
        if last == today:
            return {**base, "state": "DUE_TODAY", "label": "Due today", "days": 0, "next_due": last}
        od = (today - last).days
        return {**base, "state": "OVERDUE", "overdue_days": od, "label": f"Overdue by {od} day{'s' if od != 1 else ''}"}
    if n >= 2:
        return {**base, "state": "PAID_AHEAD", "label": f"Paid ahead. Next due {nxt:%d %b %Y}"}
    if days_next <= s.reminder_days:
        return {**base, "state": "DUE_SOON", "label": f"Due in {days_next} day{'s' if days_next != 1 else ''}"}
    return {**base, "state": "PAID", "label": f"Next due in {days_next} days"}


def overview(group, today):
    """Every active member with their countdown state (officials only)."""
    s = get_settings(group.id)
    members = GroupMember.query.join(User, User.id == GroupMember.user_id).filter(
        GroupMember.group_id == group.id, GroupMember.status == "ACTIVE").order_by(User.full_name).all()
    rows_by = {r.member_id: r for r in MemberSchedule.query.filter_by(group_id=group.id)}
    out, rev = [], _reversed_ids(group.id)
    for m in members:
        row = rows_by.get(m.id)
        st = status_for(group, m, row, today, s, rev)
        out.append({"m": m, "row": row, "st": st, "text": describe(s.contribution_period, row.due_day if row else None,
                                                                   row.due_month if row else None)})
    order = {"OVERDUE": 0, "DUE_TODAY": 1, "DUE_SOON": 2, "NO_DAY": 3, "PAID": 4, "PAID_AHEAD": 5}
    out.sort(key=lambda r: (order[r["st"]["state"]], -r["st"]["overdue_days"], r["m"].user.full_name.lower()))
    return out


def send_reminders(group, today):
    """Idempotent: members get one reminder per cycle; officials one summary per day."""
    s = get_settings(group.id)
    rows = overview(group, today)
    late = 0
    for r in rows:
        st, m = r["st"], r["m"]
        if st["state"] == "DUE_SOON":
            notify.notify([m.user_id], group.id, "REMINDER", "Contribution coming up",
                          f"Your {PERIOD_LABEL[s.contribution_period]} contribution is due on {st['next_due']:%d %b %Y}.",
                          f"/g/{group.id}/savings/me", dedupe_key=f"remind:{m.id}:{st['next_due']}")
        elif st["state"] in ("DUE_TODAY", "OVERDUE"):
            late += 1
            notify.notify([m.user_id], group.id, "REMINDER", "Contribution due",
                          "Your contribution is due. Please pay when you can.", f"/g/{group.id}/savings/me",
                          dedupe_key=f"due:{m.id}:{st['last_due']}")
    if late:
        notify.notify(notify.official_ids(group.id), group.id, "REMINDER", "Members who have not paid",
                      f"{late} member(s) have not paid their contribution yet.", f"/g/{group.id}/schedule/",
                      dedupe_key=f"unpaid:{group.id}:{today}")
    s.last_unpaid_count = late
    return rows


def run_daily(group, today):
    """Send reminders and overdue-loan alerts at most once a day per group
    (triggered by the first visit of the day). Returns the unpaid-member count."""
    from app.services import fines
    s = get_settings(group.id)
    if s.last_reminder_date == today:
        return s.last_unpaid_count
    send_reminders(group, today)
    fines.notify_overdue(group, today)
    s.last_reminder_date = today
    return s.last_unpaid_count
