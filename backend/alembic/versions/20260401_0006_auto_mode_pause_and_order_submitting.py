"""add submitting order status"""

from alembic import op


revision = "20260401_0006"
down_revision = "20260331_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'SUBMITTING'")


def downgrade() -> None:
    pass
