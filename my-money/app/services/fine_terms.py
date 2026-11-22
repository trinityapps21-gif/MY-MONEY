"""Late-fine terms: one policy per group, frozen onto every loan when the loan is created.

The group's officials set the terms in Group rules (GroupSettings.fine_*). When a loan is created it copies them, exactly
like it copies its interest rate. From then on that loan is charged under ITS OWN terms, whatever the group changes later:
a change only ever affects loans created after it. Every calculation, screen and PDF reads the terms from the loan through
this module, so they can never disagree about what a loan's terms are.

    grace      no fine for this many days after the due date
    period     after the grace period, a fine is charged for every period of this many days
    type       PERCENT of the late amount, or a FIXED amount per period
    capped     the fine never exceeds the late amount
"""
from types import SimpleNamespace

from app.services.money import fmt

FIELDS = ("fine_grace_days", "fine_period_days", "fine_type", "fine_rate_bp", "fine_fixed", "fine_capped")
DEFAULTS = {"fine_grace_days": 7, "fine_period_days": 7, "fine_type": "PERCENT", "fine_rate_bp": 500,
            "fine_fixed": 0, "fine_capped": True}
OPEN_STATES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "ACTIVE", "PARTIALLY_REPAID", "OVERDUE")


def values(obj) -> dict:
    """The six term values of a Loan, a GroupSettings row or a dict (anything missing falls back to the default)."""
    get = obj.get if isinstance(obj, dict) else (lambda k, d=None: getattr(obj, k, d))
    out = {}
    for f in FIELDS:
        v = get(f, None)
        out[f] = DEFAULTS[f] if v is None else v
    out["fine_capped"] = bool(out["fine_capped"])
    return out


def terms(obj) -> SimpleNamespace:
    """The same values with attribute access (what late_fees.assess and fines.compute read)."""
    return SimpleNamespace(**values(obj))


def snapshot(group_id) -> dict:
    """The group's current terms, ready to be copied onto a new loan: Loan(**snapshot(group.id))."""
    from app.services.groupcfg import get_settings
    return values(get_settings(group_id))


def same(a, b) -> bool:
    return values(a) == values(b)


def is_free(t) -> bool:
    """True when the terms charge nothing (a 0% rate, or a fixed amount of 0)."""
    v = values(t)
    return (v["fine_rate_bp"] if v["fine_type"] == "PERCENT" else v["fine_fixed"]) <= 0


def backfill_loans(conn) -> None:
    """One-off, run when the per-loan columns are first added: every loan that already exists is frozen on the terms its
    group is charging TODAY (its own Group rules, or the standard defaults if the group never changed them). Loans created
    before this feature therefore keep exactly the terms in force when the feature arrived, whatever the group does next."""
    from sqlalchemy import text
    sets = ", ".join(f"{f} = COALESCE((SELECT gs.{f} FROM group_settings gs WHERE gs.group_id = loans.group_id), :{f})"
                     for f in FIELDS)
    conn.execute(text(f"UPDATE loans SET {sets}"), DEFAULTS)


def _days(n) -> str:
    return f"{n} day" + ("" if n == 1 else "s")


def _what(v, currency, decimals) -> str:
    return (f"{v['fine_rate_bp'] / 100:g}% of the late amount" if v["fine_type"] == "PERCENT"
            else f"a fixed {fmt(v['fine_fixed'], currency, decimals)}")


def describe(t, currency="", decimals=0) -> str:
    """The rule in plain words, for borrowers (statements, notices, agreements) and for the officials' own screens."""
    v = values(t)
    if is_free(v):
        return "No late fine is charged."
    grace = (f"No fine is charged for the first {_days(v['fine_grace_days'])} after the due date. "
             if v["fine_grace_days"] > 0 else "The fine starts the day after the due date. ")
    every = "every day" if v["fine_period_days"] == 1 else f"every {_days(v['fine_period_days'])}"
    cap = ", never more than the late amount" if v["fine_capped"] else ""
    return f"{grace}After that, {_what(v, currency, decimals)} is charged for {every}{cap}."


def headline(t, currency="", decimals=0) -> str:
    """A one-line label for tables: '5% every 7 days after 7 days grace, capped'."""
    v = values(t)
    if is_free(v):
        return "No late fine"
    per = "day" if v["fine_period_days"] == 1 else f"{v['fine_period_days']} days"
    what = f"{v['fine_rate_bp'] / 100:g}%" if v["fine_type"] == "PERCENT" else fmt(v["fine_fixed"], currency, decimals)
    return f"{what} every {per} after {_days(v['fine_grace_days'])} grace" + (", capped" if v["fine_capped"] else "")


def changes(old, new, currency="", decimals=0) -> list[dict]:
    """What differs between two sets of terms, as [{'label', 'old', 'new'}] in words an official can read."""
    o, n = values(old), values(new)

    def show(field, v):
        if field in ("fine_grace_days", "fine_period_days"):
            return _days(v[field])
        if field == "fine_type":
            return "Percentage of the late amount" if v[field] == "PERCENT" else "Fixed amount"
        if field == "fine_rate_bp":
            return f"{v[field] / 100:g}%"
        if field == "fine_fixed":
            return fmt(v[field], currency, decimals)
        return "Yes" if v[field] else "No"

    labels = {"fine_grace_days": "Grace period", "fine_period_days": "Fine is charged every",
              "fine_type": "Fine type", "fine_rate_bp": "Percentage per period", "fine_fixed": "Fixed amount per period",
              "fine_capped": "Never more than the late amount"}
    return [{"label": labels[f], "old": show(f, o), "new": show(f, n)} for f in FIELDS if o[f] != n[f]]


def _open_loans(group_id):
    from app.models import Loan
    return Loan.query.filter(Loan.group_id == group_id, Loan.status.in_(OPEN_STATES))


def count_on_other_terms(group_id, current) -> int:
    """How many open loans of the group were created under terms that differ from `current`."""
    from sqlalchemy import or_
    from app.models import Loan
    v = values(current)
    return _open_loans(group_id).filter(or_(*[getattr(Loan, f) != v[f] for f in FIELDS])).count()


def in_force(group, current=None) -> list[dict]:
    """The terms the group's open loans are charged under, newest terms first:
    [{'values', 'headline', 'count', 'is_current', 'latest'}]. Shows an official, at a glance, that older loans keep older terms."""
    from sqlalchemy import func
    from app.extensions import db
    from app.models import Loan
    from app.services.groupcfg import get_settings
    cur = values(current if current is not None else get_settings(group.id))
    cols = [getattr(Loan, f) for f in FIELDS]
    q = (db.session.query(*cols, func.count(Loan.id), func.max(Loan.created_at))
         .filter(Loan.group_id == group.id, Loan.status.in_(OPEN_STATES)).group_by(*cols))
    rows = []
    for r in q.all():
        v = values(dict(zip(FIELDS, r[:len(FIELDS)])))
        rows.append({"values": v, "headline": headline(v, group.currency, group.currency_decimals), "count": r[-2],
                     "is_current": v == cur, "latest": r[-1]})
    if not any(x["is_current"] for x in rows):          # no loan uses the current terms yet: still show them
        rows.append({"values": cur, "headline": headline(cur, group.currency, group.currency_decimals), "count": 0,
                     "is_current": True, "latest": None})
    rows.sort(key=lambda x: (not x["is_current"], -(x["latest"].timestamp() if x["latest"] else 0)))
    return rows
