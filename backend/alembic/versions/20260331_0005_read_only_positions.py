"""add observed positions and external close status"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260331_0005"
down_revision = "20260329_0003"
branch_labels = None
depends_on = None

signal_direction_enum = postgresql.ENUM("LONG", "SHORT", name="signaldirection", create_type=False)


def upgrade() -> None:
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'CLOSED_EXTERNALLY'")

    op.create_table(
        "observed_positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("position_side", sa.String(length=20), nullable=False),
        sa.Column("direction", signal_direction_enum, nullable=False),
        sa.Column("source_kind", sa.String(length=20), nullable=False),
        sa.Column("linked_order_id", sa.Integer(), sa.ForeignKey("orders.id")),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("mark_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("leverage", sa.Integer()),
        sa.Column("unrealized_pnl", sa.Numeric(20, 8), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.UniqueConstraint("symbol", "position_side", name="uq_observed_positions_symbol_side"),
    )
    op.create_table(
        "position_pnl_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("observed_position_id", sa.Integer(), sa.ForeignKey("observed_positions.id"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("mark_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(20, 8), nullable=False),
    )
    op.create_index("ix_position_pnl_snapshots_observed_position_id", "position_pnl_snapshots", ["observed_position_id"])


def downgrade() -> None:
    op.drop_index("ix_position_pnl_snapshots_observed_position_id", table_name="position_pnl_snapshots")
    op.drop_table("position_pnl_snapshots")
    op.drop_table("observed_positions")
