"""shares, withdrawals, ranks, schedules, loan fines, income and expenses

Revision ID: a1f3c9d2e801
Revises: 5b4838f04d7d
Create Date: 2026-11-18 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a1f3c9d2e801'
down_revision = '5b4838f04d7d'
branch_labels = None
depends_on = None


def _id():
    return sa.Column('id', sa.Integer(), primary_key=True)


def _fk(col, target, nullable=False):
    return sa.Column(col, sa.Integer(), sa.ForeignKey(target), nullable=nullable)


def upgrade():
    op.add_column('transactions', sa.Column('kind', sa.String(length=20), nullable=True))

    op.create_table(
        'group_settings', _id(), _fk('group_id', 'groups.id'),
        sa.Column('min_savings_balance', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('share_price', sa.BigInteger(), nullable=False, server_default='10000'),
        sa.Column('leaderboard_on', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('contribution_period', sa.String(length=10), nullable=False, server_default='MONTHLY'),
        sa.Column('reminder_days', sa.Integer(), nullable=False, server_default='7'),
        sa.Column('last_reminder_date', sa.Date(), nullable=True),
        sa.Column('last_unpaid_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('fine_grace_days', sa.Integer(), nullable=False, server_default='7'),
        sa.Column('fine_period_days', sa.Integer(), nullable=False, server_default='7'),
        sa.Column('fine_type', sa.String(length=8), nullable=False, server_default='PERCENT'),
        sa.Column('fine_rate_bp', sa.Integer(), nullable=False, server_default='500'),
        sa.Column('fine_fixed', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('fine_capped', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('default_rate_bp', sa.Integer(), nullable=False, server_default='1000'),
        sa.Column('min_rate_bp', sa.Integer(), nullable=False, server_default='500'),
        sa.Column('max_rate_bp', sa.Integer(), nullable=False, server_default='2000'),
        sa.Column('flat_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('reducing_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('default_period', sa.String(length=10), nullable=False, server_default='MONTH'),
        sa.Column('allow_custom_rates', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("contribution_period IN ('WEEKLY','MONTHLY','ANNUAL')", name='ck_gs_period'),
        sa.CheckConstraint("fine_type IN ('PERCENT','FIXED')", name='ck_gs_fine_type'),
        sa.UniqueConstraint('group_id'),
    )

    op.create_table(
        'member_schedules', _id(), _fk('group_id', 'groups.id'), _fk('member_id', 'group_members.id'),
        sa.Column('due_day', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('due_month', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint('group_id', 'member_id', name='uq_member_schedule'),
    )
    op.create_index('ix_member_schedules_group_id', 'member_schedules', ['group_id'])

    op.create_table(
        'share_transfers', _id(), _fk('group_id', 'groups.id'), _fk('seller_id', 'group_members.id'),
        _fk('buyer_id', 'group_members.id'),
        sa.Column('units', sa.Integer(), nullable=False),
        sa.Column('unit_price', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=10), nullable=False, server_default='PENDING'),
        sa.Column('note', sa.String(length=300)),
        _fk('requested_by', 'users.id'), _fk('decided_by', 'users.id', True),
        sa.Column('decided_at', sa.DateTime()), sa.Column('decision_note', sa.String(length=300)),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint('units > 0', name='ck_transfer_units'),
        sa.CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','CANCELLED')", name='ck_transfer_status'),
    )
    op.create_index('ix_share_transfers_group_id', 'share_transfers', ['group_id'])

    op.create_table(
        'share_entries', _id(), _fk('group_id', 'groups.id'), _fk('member_id', 'group_members.id'),
        sa.Column('kind', sa.String(length=14), nullable=False),
        sa.Column('units', sa.Integer(), nullable=False),
        sa.Column('unit_price', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('amount', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('entry_date', sa.Date(), nullable=False),
        sa.Column('note', sa.String(length=300)),
        _fk('tx_id', 'transactions.id', True), _fk('transfer_id', 'share_transfers.id', True),
        sa.Column('receipt_no', sa.String(length=30)),
        _fk('recorded_by', 'users.id'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column('reversed_at', sa.DateTime()), sa.Column('reversal_reason', sa.String(length=300)),
        sa.CheckConstraint("kind IN ('PURCHASE','TRANSFER_OUT','TRANSFER_IN')", name='ck_share_kind'),
    )
    op.create_index('ix_share_entries_group_id', 'share_entries', ['group_id'])
    op.create_index('ix_share_entries_member_id', 'share_entries', ['member_id'])

    op.create_table(
        'share_ranks', _id(), _fk('group_id', 'groups.id'),
        sa.Column('name', sa.String(length=40), nullable=False),
        sa.Column('min_value', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('icon', sa.String(length=30), nullable=False, server_default='sprout'),
    )
    op.create_index('ix_share_ranks_group_id', 'share_ranks', ['group_id'])

    op.create_table(
        'savings_withdrawals', _id(), _fk('group_id', 'groups.id'), _fk('member_id', 'group_members.id'),
        sa.Column('amount', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False, server_default='PENDING'),
        sa.Column('note', sa.String(length=300)), sa.Column('payment_method', sa.String(length=40)),
        _fk('requested_by', 'users.id'), _fk('decided_by', 'users.id', True),
        sa.Column('decided_at', sa.DateTime()), sa.Column('decision_note', sa.String(length=300)),
        _fk('tx_id', 'transactions.id', True), sa.Column('receipt_no', sa.String(length=30)),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint('amount > 0', name='ck_wd_amount'),
        sa.CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','CANCELLED','REVERSED')", name='ck_wd_status'),
    )
    op.create_index('ix_savings_withdrawals_group_id', 'savings_withdrawals', ['group_id'])
    op.create_index('ix_savings_withdrawals_member_id', 'savings_withdrawals', ['member_id'])

    op.create_table(
        'loan_promises', _id(), _fk('group_id', 'groups.id'), _fk('loan_id', 'loans.id'),
        sa.Column('promise_date', sa.Date(), nullable=False),
        sa.Column('amount', sa.BigInteger(), nullable=False),
        sa.Column('note', sa.String(length=300)),
        sa.Column('status', sa.String(length=8), nullable=False, server_default='OPEN'),
        _fk('created_by', 'users.id'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("status IN ('OPEN','KEPT','CANCELLED')", name='ck_promise_status'),
    )
    op.create_index('ix_loan_promises_group_id', 'loan_promises', ['group_id'])
    op.create_index('ix_loan_promises_loan_id', 'loan_promises', ['loan_id'])

    op.create_table(
        'loan_fines', _id(), _fk('group_id', 'groups.id'), _fk('loan_id', 'loans.id'),
        _fk('borrower_id', 'borrowers.id'), _fk('repayment_id', 'loan_repayments.id', True),
        sa.Column('deadline', sa.Date(), nullable=False), sa.Column('paid_on', sa.Date(), nullable=False),
        sa.Column('days_late', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('late_amount', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('amount', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False, server_default='UNPAID'),
        sa.Column('reason', sa.String(length=300)),
        _fk('tx_id', 'transactions.id', True), sa.Column('settled_on', sa.Date()),
        _fk('decided_by', 'users.id', True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint('amount >= 0', name='ck_fine_amount'),
        sa.CheckConstraint("status IN ('UNPAID','PAID','WAIVED','VOID')", name='ck_fine_status'),
    )
    op.create_index('ix_loan_fines_group_id', 'loan_fines', ['group_id'])
    op.create_index('ix_loan_fines_loan_id', 'loan_fines', ['loan_id'])

    op.create_table(
        'income_categories', _id(), _fk('group_id', 'groups.id'),
        sa.Column('name', sa.String(length=60), nullable=False),
        sa.UniqueConstraint('group_id', 'name', name='uq_income_cat'),
    )
    op.create_index('ix_income_categories_group_id', 'income_categories', ['group_id'])

    op.create_table(
        'income_records', _id(), _fk('group_id', 'groups.id'), _fk('category_id', 'income_categories.id'),
        sa.Column('amount', sa.BigInteger(), nullable=False),
        sa.Column('income_date', sa.Date(), nullable=False),
        sa.Column('description', sa.String(length=300)),
        sa.Column('tx_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=False, unique=True),
        _fk('recorded_by', 'users.id'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint('amount > 0', name='ck_income_amount'),
    )
    op.create_index('ix_income_records_group_id', 'income_records', ['group_id'])

    op.create_table(
        'expense_categories', _id(), _fk('group_id', 'groups.id'),
        sa.Column('name', sa.String(length=60), nullable=False),
        sa.UniqueConstraint('group_id', 'name', name='uq_expense_cat'),
    )
    op.create_index('ix_expense_categories_group_id', 'expense_categories', ['group_id'])

    op.create_table(
        'expense_items', _id(), _fk('group_id', 'groups.id'), _fk('expense_id', 'expenses.id'),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('amount', sa.BigInteger(), nullable=False),
        sa.CheckConstraint('amount > 0', name='ck_item_amount'),
    )
    op.create_index('ix_expense_items_group_id', 'expense_items', ['group_id'])
    op.create_index('ix_expense_items_expense_id', 'expense_items', ['expense_id'])


def downgrade():
    for t in ('expense_items', 'expense_categories', 'income_records', 'income_categories', 'loan_fines',
              'loan_promises', 'savings_withdrawals', 'share_ranks', 'share_entries', 'share_transfers',
              'member_schedules', 'group_settings'):
        op.drop_table(t)
    op.drop_column('transactions', 'kind')
