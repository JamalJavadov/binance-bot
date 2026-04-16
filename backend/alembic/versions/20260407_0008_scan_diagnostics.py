"""add scan diagnostics context and restore auto-only defaults"""

from alembic import op
import sqlalchemy as sa


revision = "20260407_0008"
down_revision = "20260402_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scan_symbol_results",
        sa.Column("extra_context", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO settings (key, value)
            VALUES
              ('risk_per_trade_pct', '2.0'),
              ('max_portfolio_risk_pct', '6.0'),
              ('max_leverage', '10'),
              ('deployable_equity_pct', '90'),
              ('max_book_spread_bps', '12'),
              ('min_24h_quote_volume_usdt', '25000000'),
              ('kill_switch_consecutive_stop_losses', '2'),
              ('kill_switch_daily_drawdown_pct', '4.0'),
              ('auto_mode_max_entry_drift_pct', '5.0')
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value
            """
        )
    )


def downgrade() -> None:
    op.drop_column("scan_symbol_results", "extra_context")
