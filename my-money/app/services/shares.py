"""Shares: purchases entered by officials, transfers between members of the
same group, and the share-value ranks (leaderboard shows ranks, never amounts)."""
from sqlalchemy import func

from app.extensions import db
from app.models import GroupMember, ShareEntry, ShareRank, ShareTransfer, Transaction, User, utcnow
from app.services import ledger, notify
from app.services.groupcfg import get_settings

DEFAULT_RANKS = [("Seedling", 1, "sprout"), ("Saver", 100_000, "coins"), ("Grower", 500_000, "trees"),
                 ("Builder", 1_000_000, "hammer"), ("Champion", 5_000_000, "trophy"),
                 ("Legend", 10_000_000, "crown")]
RANK_ICONS = ["sprout", "coins", "trees", "hammer", "trophy", "crown", "star", "gem", "medal", "award", "flame",
              "rocket", "shield", "sun", "zap"]


class ShareError(ValueError):
    pass


# ------------------------------------------------------------------ holdings
def holdings(group_id) -> dict:
    rows = db.session.query(ShareEntry.member_id, func.coalesce(func.sum(ShareEntry.units), 0)).filter(
        ShareEntry.group_id == group_id, ShareEntry.reversed_at.is_(None)).group_by(ShareEntry.member_id).all()
    return {m: int(u) for m, u in rows}


def member_units(group_id, member_id) -> int:
    return holdings(group_id).get(member_id, 0)


def value(group_id, units) -> int:
    return int(units) * get_settings(group_id).share_price


# --------------------------------------------------------------------- ranks
def ensure_ranks(group_id):
    ranks = ShareRank.query.filter_by(group_id=group_id).order_by(ShareRank.min_value).all()
    if not ranks:
        for name, mn, icon in DEFAULT_RANKS:
            db.session.add(ShareRank(group_id=group_id, name=name, min_value=mn, icon=icon))
        db.session.flush()
        ranks = ShareRank.query.filter_by(group_id=group_id).order_by(ShareRank.min_value).all()
    return ranks


def rank_for(group_id, units, ranks=None):
    """(current_rank | None, next_rank | None, progress_value, value)"""
    ranks = ranks or ensure_ranks(group_id)
    val = value(group_id, units)
    cur = None
    for r in ranks:
        if val >= r.min_value and units > 0:
            cur = r
    nxt = next((r for r in ranks if r.min_value > val), None)
    return cur, nxt, val


def leaderboard(group_id):
    """Ranks only: [{name, rank, icon}] best first. No amounts or share counts."""
    ranks = ensure_ranks(group_id)
    held = holdings(group_id)
    members = GroupMember.query.join(User, User.id == GroupMember.user_id).filter(
        GroupMember.group_id == group_id, GroupMember.status == "ACTIVE").all()
    out = []
    for m in members:
        units = held.get(m.id, 0)
        cur, _n, val = rank_for(group_id, units, ranks)
        out.append({"member_id": m.id, "name": m.user.full_name, "user": m.user, "rank": cur.name if cur else None,
                    "icon": cur.icon if cur else "circle-dashed", "_v": val})
    out.sort(key=lambda r: (-r["_v"], r["name"].lower()))
    for r in out:
        r.pop("_v")
    return out


def rank_distribution(group_id):
    ranks = ensure_ranks(group_id)
    held = holdings(group_id)
    counts = {r.name: 0 for r in ranks}
    members = GroupMember.query.filter_by(group_id=group_id, status="ACTIVE").all()
    for m in members:
        cur, _n, _v = rank_for(group_id, held.get(m.id, 0), ranks)
        if cur:
            counts[cur.name] += 1
    return [(r.name, counts[r.name]) for r in ranks]


def _rank_name(group_id, units):
    cur, _n, _v = rank_for(group_id, units)
    return cur.name if cur else None


def _announce_rank(group, member, before_name, after_units):
    after = _rank_name(group.id, after_units)
    if after and after != before_name:
        notify.notify([member.user_id], group.id, "RANK", f"You reached {after}!",
                      f"Your shares have taken you to the {after} rank. Congratulations!",
                      f"/g/{group.id}/shares/me")
        notify.notify_officials(group.id, "RANK", "Rank up", f"{member.user.full_name} reached {after}.",
                                f"/g/{group.id}/shares/", exclude=member.user_id)


# ------------------------------------------------------------------ purchases
def record_purchase(group, user, member, units, entry_date, unit_price=None, note=None, method=None):
    if member.group_id != group.id or member.status != "ACTIVE":
        raise ShareError("Select an active member of this group.")
    if units <= 0:
        raise ShareError("Enter the number of shares (a whole number above zero).")
    price = unit_price if unit_price and unit_price > 0 else get_settings(group.id).share_price
    if price <= 0:
        raise ShareError("Set a share price in Share settings first.")
    amount = units * price
    before_units = member_units(group.id, member.id)
    before_rank = _rank_name(group.id, before_units)
    tx = ledger.post(group.id, "ADJUSTMENT", amount, "IN", entry_date, user.id,
                     f"Share purchase: {units} share(s) - {member.user.full_name}", method, None,
                     member_id=member.id, prefix="SHR", kind="SHARE_PURCHASE")
    e = ShareEntry(group_id=group.id, member_id=member.id, kind="PURCHASE", units=units, unit_price=price,
                   amount=amount, entry_date=entry_date, note=(note or "")[:300], tx_id=tx.id,
                   receipt_no=ledger.new_ref("SH"), recorded_by=user.id)
    db.session.add(e)
    db.session.flush()
    notify.notify([member.user_id], group.id, "SHARES", "Shares recorded",
                  f"{units} share(s) were added to your account. Receipt {e.receipt_no}.", f"/g/{group.id}/shares/me")
    _announce_rank(group, member, before_rank, before_units + units)
    return e


def units_for_amount(group_id, amount, unit_price=None):
    """(whole shares, price per share, left over) for an amount of money. The left over is less than one share."""
    price = unit_price if unit_price and unit_price > 0 else get_settings(group_id).share_price
    if price <= 0:
        raise ShareError("Set a share price in Share settings first.")
    if amount <= 0:
        raise ShareError("Enter an amount above zero.")
    units = amount // price
    return units, price, amount - units * price


def record_purchase_by_amount(group, user, member, amount, entry_date, unit_price=None, note=None, method=None):
    """The member paid `amount`: work out the whole shares it buys at the share price and save them.
    Only whole shares are recorded (units x price); returns (entry, left_over) so the caller can tell the official
    about any money that does not make a full share."""
    units, price, left = units_for_amount(group.id, amount, unit_price)
    if units < 1:
        raise ShareError(f"That amount is less than the price of one share ({price / 10 ** group.currency_decimals:,.{group.currency_decimals}f} {group.currency}).")
    entry = record_purchase(group, user, member, units, entry_date, price, note, method)
    return entry, left


def reverse_purchase(group, entry: ShareEntry, user, reason, today):
    if entry.group_id != group.id or entry.kind != "PURCHASE" or entry.reversed_at:
        raise ShareError("This share purchase cannot be reversed.")
    if member_units(group.id, entry.member_id) - entry.units < 0:
        raise ShareError("The member has already transferred some of these shares.")
    ledger.reverse(db.session.get(Transaction, entry.tx_id), user.id, reason, today)
    entry.reversed_at = utcnow()
    entry.reversal_reason = reason.strip()[:300]


# ------------------------------------------------------------------ transfers
def request_transfer(group, user, seller, buyer, units, unit_price, note=None):
    for m in (seller, buyer):
        if m is None or m.group_id != group.id or m.status != "ACTIVE":
            raise ShareError("Both people must be active members of this group.")
    if seller.id == buyer.id:
        raise ShareError("Choose a different member to receive the shares.")
    if units <= 0:
        raise ShareError("Enter the number of shares to transfer.")
    if units > member_units(group.id, seller.id):
        raise ShareError("The seller does not have that many shares.")
    t = ShareTransfer(group_id=group.id, seller_id=seller.id, buyer_id=buyer.id, units=units,
                      unit_price=max(0, unit_price or 0), note=(note or "")[:300], requested_by=user.id)
    db.session.add(t)
    db.session.flush()
    notify.notify_officials(group.id, "SHARES", "Share transfer to approve",
                            f"{seller.user.full_name} wants to transfer {units} share(s) to {buyer.user.full_name}.",
                            f"/g/{group.id}/shares/transfers", exclude=user.id)
    return t


def approve_transfer(group, t: ShareTransfer, user, today):
    if t.group_id != group.id or t.status != "PENDING":
        raise ShareError("This transfer is no longer pending.")
    if t.units > member_units(group.id, t.seller_id):
        raise ShareError("The seller no longer has enough shares.")
    b_before, s_before = member_units(group.id, t.buyer_id), member_units(group.id, t.seller_id)
    b_rank, s_rank = _rank_name(group.id, b_before), _rank_name(group.id, s_before)
    amt = t.units * t.unit_price
    db.session.add(ShareEntry(group_id=group.id, member_id=t.seller_id, kind="TRANSFER_OUT", units=-t.units,
                              unit_price=t.unit_price, amount=amt, entry_date=today, transfer_id=t.id,
                              note=f"Transferred to {t.buyer.user.full_name}", recorded_by=user.id))
    db.session.add(ShareEntry(group_id=group.id, member_id=t.buyer_id, kind="TRANSFER_IN", units=t.units,
                              unit_price=t.unit_price, amount=amt, entry_date=today, transfer_id=t.id,
                              note=f"Received from {t.seller.user.full_name}", recorded_by=user.id))
    t.status, t.decided_by, t.decided_at = "APPROVED", user.id, utcnow()
    db.session.flush()
    notify.notify([t.seller.user_id, t.buyer.user_id], group.id, "SHARES", "Share transfer approved",
                  f"{t.units} share(s) moved from {t.seller.user.full_name} to {t.buyer.user.full_name}.",
                  f"/g/{group.id}/shares/me")
    _announce_rank(group, t.buyer, b_rank, b_before + t.units)
    _announce_rank(group, t.seller, s_rank, s_before - t.units)


def decide_transfer(group, t, user, approve, note=None, today=None):
    if approve:
        return approve_transfer(group, t, user, today)
    if t.group_id != group.id or t.status != "PENDING":
        raise ShareError("This transfer is no longer pending.")
    t.status, t.decided_by, t.decided_at, t.decision_note = "REJECTED", user.id, utcnow(), (note or "")[:300]
    notify.notify([t.seller.user_id, t.buyer.user_id], group.id, "SHARES", "Share transfer declined",
                  "The share transfer was not approved.", f"/g/{group.id}/shares/me")
