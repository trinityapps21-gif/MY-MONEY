"""full-workbook import: ImportBatch.bundle groups the sheets of one workbook

Revision ID: a7d9f1c3b568
Revises: f6b8d0e2a445
Safe to run even if `flask sync-schema` already added the column (it checks first).
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7d9f1c3b568'
down_revision = 'f6b8d0e2a445'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if 'bundle' not in {c['name'] for c in insp.get_columns('import_batches')}:
        op.add_column('import_batches', sa.Column('bundle', sa.String(length=24), nullable=True))
    if 'ix_import_batches_bundle' not in {i['name'] for i in insp.get_indexes('import_batches')}:
        op.create_index('ix_import_batches_bundle', 'import_batches', ['bundle'])


def downgrade():
    op.drop_index('ix_import_batches_bundle', table_name='import_batches')
    with op.batch_alter_table('import_batches') as b:
        b.drop_column('bundle')
