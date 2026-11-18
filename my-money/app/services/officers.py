from app.models import GroupMember, OFFICIAL_ROLES, User


def officer_options(group_id):
    """Active staff who can be assigned borrowers and loans: [(user_id, name)]."""
    rows = (GroupMember.query.filter(GroupMember.group_id == group_id, GroupMember.status == "ACTIVE",
                                     GroupMember.role.in_(OFFICIAL_ROLES)).all())
    users = {u.id: u for u in User.query.filter(User.id.in_([m.user_id for m in rows])).all()}
    return sorted(((m.user_id, users[m.user_id].full_name) for m in rows if m.user_id in users), key=lambda r: r[1])


def valid_officer(group_id, user_id):
    return any(uid == user_id for uid, _ in officer_options(group_id))
