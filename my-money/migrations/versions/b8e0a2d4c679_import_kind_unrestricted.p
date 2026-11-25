"""allow every kind of import sheet: drop the old ck_import_kind CHECK on import_batches

Revision ID: b8e0a2d4c679
Revises: a7d9f1c3b568
The first version of the table only allowed members / contributions / loans / repayments, so Shares, Income and
Expenses sheets (and the full-workbook import) failed. SQLite cannot drop a constraint in place, so the table is
rebuilt without it. Does nothing when `flask sync-schema` has already done it.
"""
from alembic import op

revision = 'b8e0a2d4c679'
down_revision = 'a7d9f1c3b568'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        from app.schema_fixes import drop_import_kind_check
        drop_import_kind_check(bind)


def downgrade():
    pass   # the constraint is not restored: it would only block imports that are valid
