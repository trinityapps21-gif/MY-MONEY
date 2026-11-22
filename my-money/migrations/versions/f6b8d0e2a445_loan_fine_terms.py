"""per-loan late-fine terms (frozen when the loan is created) and the date a group last changed them

Revision ID: f6b8d0e2a445
Revises: e5a7c9b1d334
Safe to run even if `flask sync-schema` already added the columns (it checks first).

Every loan that already exists is frozen on the late-fine terms its group is charging today (its own Group rules, or the
standard 7-day grace / 5% every 7 days / capped defaults), so a group that changes its fine rules afterwards only affects
loans created after the change.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f6b8d0e2a445'
down_revision = 'e5a7c9b1d334'
branch_labels = None
depends_on = None

LOAN_COLS = [
    ('fine_grace_days', sa.Column('fine_grace_days', sa.Integer(), nullable=False, server_default='7')),
    ('fine_period_days', sa.Column('fine_period_days', sa.Integer(), nullable=False, server_default='7')),
    ('fine_type', sa.Column('fine_type', sa.String(length=8), nullable=False, server_default='PERCENT')),
    ('fine_rate_bp', sa.Column('fine_rate_bp', sa.Integer(), nullable=False, server_default='500')),
    ('fine_fixed', sa.Column('fine_fixed', sa.BigInteger(), nullable=False, server_default='0')),
    ('fine_capped', sa.Column('fine_capped', sa.Boolean(), nullable=False, server_default='1')),
]


def upgrade():
    insp = sa.inspect(op.get_bind())
    have_loans = {c['name'] for c in insp.get_columns('loans')}
    added_on_loans = False
    for name, col in LOAN_COLS:
        if name not in have_loans:
            op.add_column('loans', col)
            added_on_loans = True
    have_settings = {c['name'] for c in insp.get_columns('group_settings')}
    if 'fine_terms_since' not in have_settings:
        op.add_column('group_settings', sa.Column('fine_terms_since', sa.DateTime(), nullable=True))
    if added_on_loans:
        from app.services import fine_terms
        fine_terms.backfill_loans(op.get_bind())


def downgrade():
    with op.batch_alter_table('group_settings') as b:
        b.drop_column('fine_terms_since')
    with op.batch_alter_table('loans') as b:
        for name, _ in reversed(LOAN_COLS):
            b.drop_column(name)
