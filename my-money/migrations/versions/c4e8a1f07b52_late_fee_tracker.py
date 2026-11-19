"""late fee tracker (admin alerts per penalty block)

Revision ID: c4e8a1f07b52
Revises: b7c2d9e41f03
"""
from alembic import op
import sqlalchemy as sa

revision = 'c4e8a1f07b52'
down_revision = 'b7c2d9e41f03'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'late_fee_tracker',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('loan_id', sa.Integer(), nullable=False),
        sa.Column('schedule_id', sa.Integer(), nullable=False),
        sa.Column('last_block', sa.Integer(), nullable=False),
        sa.Column('last_fine', sa.BigInteger(), nullable=False),
        sa.Column('last_notified_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id']),
        sa.ForeignKeyConstraint(['loan_id'], ['loans.id']),
        sa.ForeignKeyConstraint(['schedule_id'], ['loan_repayment_schedules.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('schedule_id'),
    )
    op.create_index('ix_late_fee_tracker_group_id', 'late_fee_tracker', ['group_id'])
    op.create_index('ix_late_fee_tracker_loan_id', 'late_fee_tracker', ['loan_id'])


def downgrade():
    op.drop_index('ix_late_fee_tracker_loan_id', table_name='late_fee_tracker')
    op.drop_index('ix_late_fee_tracker_group_id', table_name='late_fee_tracker')
    op.drop_table('late_fee_tracker')
