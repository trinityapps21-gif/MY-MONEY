"""web push subscriptions (pop-up notifications on phones and laptops)

Revision ID: c9f1b3d5e780
Revises: b8e0a2d4c679
Does nothing when `flask sync-schema` has already created the table.
"""
import sqlalchemy as sa
from alembic import op

revision = 'c9f1b3d5e780'
down_revision = 'b8e0a2d4c679'
branch_labels = None
depends_on = None


def upgrade():
    if "push_subscriptions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("endpoint", sa.String(600), nullable=False, unique=True),
        sa.Column("p256dh", sa.String(200), nullable=False),
        sa.Column("auth", sa.String(60), nullable=False),
        sa.Column("user_agent", sa.String(200)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_sent_at", sa.DateTime()),
    )
    op.create_index("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"])


def downgrade():
    op.drop_table("push_subscriptions")
