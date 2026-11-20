"""client media (ID cards, permits, passports, asset photos) + lender requirement flag

Revision ID: d4f6b8c0e223
Revises: c4e8a1f07b52
Safe to run even if `flask sync-schema` already created these (it checks first).
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4f6b8c0e223'
down_revision = 'c4e8a1f07b52'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if 'client_media' not in insp.get_table_names():
        op.create_table(
            'client_media',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('group_id', sa.Integer(), nullable=False),
            sa.Column('borrower_id', sa.Integer(), nullable=False),
            sa.Column('loan_id', sa.Integer(), nullable=True),
            sa.Column('collateral_id', sa.Integer(), nullable=True),
            sa.Column('category', sa.String(length=20), nullable=False),
            sa.Column('file_id', sa.Integer(), nullable=False),
            sa.Column('caption', sa.String(length=200), nullable=True),
            sa.Column('created_by', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['group_id'], ['groups.id']),
            sa.ForeignKeyConstraint(['borrower_id'], ['borrowers.id']),
            sa.ForeignKeyConstraint(['loan_id'], ['loans.id']),
            sa.ForeignKeyConstraint(['collateral_id'], ['collaterals.id']),
            sa.ForeignKeyConstraint(['file_id'], ['stored_files.id']),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_client_media_group_id', 'client_media', ['group_id'])
        op.create_index('ix_client_media_borrower_id', 'client_media', ['borrower_id'])
        op.create_index('ix_client_media_loan_id', 'client_media', ['loan_id'])
    cols = {c['name'] for c in insp.get_columns('groups')}
    if 'require_client_media' not in cols:
        op.add_column('groups', sa.Column('require_client_media', sa.Boolean(), nullable=False, server_default='1'))


def downgrade():
    op.drop_table('client_media')
    op.drop_column('groups', 'require_client_media')
