"""redemption sent_at

Revision ID: a41c7f09be22
Revises: df2d8eadf851
Create Date: 2026-08-10 23:30:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'a41c7f09be22'
down_revision = 'df2d8eadf851'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'redemptions',
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('redemptions', 'sent_at')
