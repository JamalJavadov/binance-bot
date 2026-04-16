"""add auto mode fields and enums"""

from alembic import op
import sqlalchemy as sa


revision = "20260329_0003"
down_revision = "20260328_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'CLOSED_BY_BOT'")
    op.add_column("orders", sa.Column("risk_budget_usdt", sa.Numeric(20, 8), nullable=False, server_default="0"))
    op.add_column("orders", sa.Column("risk_usdt_at_stop", sa.Numeric(20, 8), nullable=False, server_default="0"))
    op.add_column("orders", sa.Column("risk_pct_of_wallet", sa.Numeric(10, 4), nullable=False, server_default="0"))
    op.alter_column("orders", "risk_budget_usdt", server_default=None)
    op.alter_column("orders", "risk_usdt_at_stop", server_default=None)
    op.alter_column("orders", "risk_pct_of_wallet", server_default=None)


def downgrade() -> None:
    op.drop_column("orders", "risk_pct_of_wallet")
    op.drop_column("orders", "risk_usdt_at_stop")
    op.drop_column("orders", "risk_budget_usdt")
