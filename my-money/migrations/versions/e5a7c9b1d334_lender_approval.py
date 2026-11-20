"""lending business approval fields on groups

Revision ID: e5a7c9b1d334
Revises: d4f6b8c0e223
Safe to run even if `flask sync-schema` already added the columns (it checks first).
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5a7c9b1d334'
down_revision = 'd4f6b8c0e223'
branch_labels = None
depends_on = None

COLS = [
    ('approval_status', sa.Column('approval_status', sa.String(length=12), nullable=False, server_default='APPROVED')),
    ('business_kind', sa.Column('business_kind', sa.String(length=40), nullable=True)),
    ('district', sa.Column('district', sa.String(length=80), nullable=True)),
    ('owner_national_id', sa.Column('owner_national_id', sa.String(length=40), nullable=True)),
    ('owner_id_file_id', sa.Column('owner_id_file_id', sa.Integer(), nullable=True)),
    ('rejection_reason', sa.Column('rejection_reason', sa.String(length=300), nullable=True)),
]


def upgrade():
    have = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('groups')}
    for name, col in COLS:
        if name not in have:
            op.add_column('groups', col)


def downgrade():
    for name, _ in COLS:
        op.drop_column('groups', name)
