"""add aqrr typed metadata and trade stats table"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260410_0009"
down_revision = "20260407_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    signal_direction_enum = postgresql.ENUM("LONG", "SHORT", name="signaldirection", create_type=False)

    op.add_column("signals", sa.Column("rank_value", sa.Numeric(10, 4), nullable=True))
    op.add_column("signals", sa.Column("net_r_multiple", sa.Numeric(10, 4), nullable=True))
    op.add_column("signals", sa.Column("estimated_cost", sa.Numeric(20, 8), nullable=True))
    op.add_column("signals", sa.Column("entry_style", sa.String(length=20), nullable=True))
    op.add_column("signals", sa.Column("setup_family", sa.String(length=50), nullable=True))
    op.add_column("signals", sa.Column("setup_variant", sa.String(length=100), nullable=True))
    op.add_column("signals", sa.Column("market_state", sa.String(length=50), nullable=True))
    op.add_column("signals", sa.Column("execution_tier", sa.String(length=20), nullable=True))
    op.add_column("signals", sa.Column("score_band", sa.String(length=20), nullable=True))
    op.add_column("signals", sa.Column("volatility_band", sa.String(length=20), nullable=True))
    op.add_column("signals", sa.Column("stats_bucket_key", sa.String(length=255), nullable=True))
    op.add_column(
        "signals",
        sa.Column("strategy_context", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.alter_column("signals", "strategy_context", server_default=None)

    op.add_column("orders", sa.Column("rank_value", sa.Numeric(10, 4), nullable=True))
    op.add_column("orders", sa.Column("net_r_multiple", sa.Numeric(10, 4), nullable=True))
    op.add_column("orders", sa.Column("estimated_cost", sa.Numeric(20, 8), nullable=True))
    op.add_column("orders", sa.Column("entry_style", sa.String(length=20), nullable=True))
    op.add_column("orders", sa.Column("setup_family", sa.String(length=50), nullable=True))
    op.add_column("orders", sa.Column("setup_variant", sa.String(length=100), nullable=True))
    op.add_column("orders", sa.Column("market_state", sa.String(length=50), nullable=True))
    op.add_column("orders", sa.Column("execution_tier", sa.String(length=20), nullable=True))
    op.add_column("orders", sa.Column("score_band", sa.String(length=20), nullable=True))
    op.add_column("orders", sa.Column("volatility_band", sa.String(length=20), nullable=True))
    op.add_column("orders", sa.Column("stats_bucket_key", sa.String(length=255), nullable=True))
    op.add_column(
        "orders",
        sa.Column("strategy_context", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.alter_column("orders", "strategy_context", server_default=None)

    op.create_table(
        "aqrr_trade_stats",
        sa.Column("bucket_key", sa.String(length=255), primary_key=True),
        sa.Column("setup_family", sa.String(length=50), nullable=False),
        sa.Column("direction", signal_direction_enum, nullable=False),
        sa.Column("market_state", sa.String(length=50), nullable=False),
        sa.Column("score_band", sa.String(length=20), nullable=False),
        sa.Column("volatility_band", sa.String(length=20), nullable=False),
        sa.Column("execution_tier", sa.String(length=20), nullable=False),
        sa.Column("closed_trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("win_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("loss_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
    )
    op.alter_column("aqrr_trade_stats", "closed_trade_count", server_default=None)
    op.alter_column("aqrr_trade_stats", "win_count", server_default=None)
    op.alter_column("aqrr_trade_stats", "loss_count", server_default=None)


def downgrade() -> None:
    op.drop_table("aqrr_trade_stats")

    op.drop_column("orders", "strategy_context")
    op.drop_column("orders", "stats_bucket_key")
    op.drop_column("orders", "volatility_band")
    op.drop_column("orders", "score_band")
    op.drop_column("orders", "execution_tier")
    op.drop_column("orders", "market_state")
    op.drop_column("orders", "setup_variant")
    op.drop_column("orders", "setup_family")
    op.drop_column("orders", "entry_style")
    op.drop_column("orders", "estimated_cost")
    op.drop_column("orders", "net_r_multiple")
    op.drop_column("orders", "rank_value")

    op.drop_column("signals", "strategy_context")
    op.drop_column("signals", "stats_bucket_key")
    op.drop_column("signals", "volatility_band")
    op.drop_column("signals", "score_band")
    op.drop_column("signals", "execution_tier")
    op.drop_column("signals", "market_state")
    op.drop_column("signals", "setup_variant")
    op.drop_column("signals", "setup_family")
    op.drop_column("signals", "entry_style")
    op.drop_column("signals", "estimated_cost")
    op.drop_column("signals", "net_r_multiple")
    op.drop_column("signals", "rank_value")
