"""add partial tp order fields and drift tracking table"""

from alembic import op
import sqlalchemy as sa


revision = "20260402_0007"
down_revision = "20260401_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("partial_tp_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("orders", sa.Column("take_profit_1", sa.Numeric(20, 8), nullable=True))
    op.add_column("orders", sa.Column("take_profit_2", sa.Numeric(20, 8), nullable=True))
    op.add_column("orders", sa.Column("tp_quantity_1", sa.Numeric(20, 8), nullable=True))
    op.add_column("orders", sa.Column("tp_quantity_2", sa.Numeric(20, 8), nullable=True))
    op.add_column("orders", sa.Column("tp_order_1_id", sa.String(length=100), nullable=True))
    op.add_column("orders", sa.Column("tp_order_2_id", sa.String(length=100), nullable=True))
    op.add_column("orders", sa.Column("tp1_filled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("remaining_quantity", sa.Numeric(20, 8), nullable=True))
    op.alter_column("orders", "partial_tp_enabled", server_default=None)

    op.create_table(
        "auto_mode_drift_symbols",
        sa.Column("symbol", sa.String(length=50), primary_key=True),
        sa.Column("planned_entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("miss_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_cancelled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
    )
    op.alter_column("auto_mode_drift_symbols", "miss_count", server_default=None)


def downgrade() -> None:
    op.drop_table("auto_mode_drift_symbols")
    op.drop_column("orders", "remaining_quantity")
    op.drop_column("orders", "tp1_filled_at")
    op.drop_column("orders", "tp_order_2_id")
    op.drop_column("orders", "tp_order_1_id")
    op.drop_column("orders", "tp_quantity_2")
    op.drop_column("orders", "tp_quantity_1")
    op.drop_column("orders", "take_profit_2")
    op.drop_column("orders", "take_profit_1")
    op.drop_column("orders", "partial_tp_enabled")
