from app.models import GroupMember, User


def member_options(group_id, models=False):
    rows = GroupMember.query.join(User, User.id == GroupMember.user_id).filter(
        GroupMember.group_id == group_id, GroupMember.status == "ACTIVE").order_by(User.full_name).all()
    return rows if models else [(m.id, m.user.full_name) for m in rows]
