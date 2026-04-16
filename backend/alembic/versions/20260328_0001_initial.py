"""initial schema"""

from alembic import op
import sqlalchemy as sa


revision = "20260328_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("private_key_pem", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )
    op.create_table(
        "scan_cycles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Enum("RUNNING", "COMPLETE", "FAILED", name="scanstatus"), nullable=False),
        sa.Column("symbols_scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signals_qualified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trigger_type", sa.Enum("AUTO_MODE", name="triggertype"), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("progress_pct", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scan_cycle_id", sa.Integer(), sa.ForeignKey("scan_cycles.id")),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("direction", sa.Enum("LONG", "SHORT", name="signaldirection"), nullable=False),
        sa.Column("timeframe", sa.String(length=10), nullable=False, server_default="4h"),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("stop_loss", sa.Numeric(20, 8), nullable=False),
        sa.Column("take_profit", sa.Numeric(20, 8), nullable=False),
        sa.Column("rr_ratio", sa.Numeric(8, 2), nullable=False),
        sa.Column("confirmation_score", sa.Integer(), nullable=False),
        sa.Column("final_score", sa.Integer(), nullable=False),
        sa.Column("score_breakdown", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("reason_text", sa.Text()),
        sa.Column("swing_origin", sa.Numeric(20, 8)),
        sa.Column("swing_terminus", sa.Numeric(20, 8)),
        sa.Column("fib_0786_level", sa.Numeric(20, 8)),
        sa.Column("current_price_at_signal", sa.Numeric(20, 8)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Enum("CANDIDATE", "QUALIFIED", "DISMISSED", "APPROVED", "EXPIRED", "INVALIDATED", "ORDER_FAILED", name="signalstatus"), nullable=False),
        sa.Column("extra_context", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("signal_id", sa.Integer(), sa.ForeignKey("signals.id")),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("direction", sa.Enum("LONG", "SHORT", name="signaldirection"), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("margin_type", sa.String(length=20), nullable=False, server_default="ISOLATED"),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("stop_loss", sa.Numeric(20, 8), nullable=False),
        sa.Column("take_profit", sa.Numeric(20, 8), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("position_margin", sa.Numeric(20, 8), nullable=False),
        sa.Column("notional_value", sa.Numeric(20, 8), nullable=False),
        sa.Column("rr_ratio", sa.Numeric(8, 2), nullable=False),
        sa.Column("entry_order_id", sa.String(length=100)),
        sa.Column("tp_order_id", sa.String(length=100)),
        sa.Column("sl_order_id", sa.String(length=100)),
        sa.Column("status", sa.Enum("PENDING_APPROVAL", "ORDER_PLACED", "IN_POSITION", "CLOSED_WIN", "CLOSED_LOSS", "CANCELLED_BY_BOT", "CANCELLED_BY_USER", name="orderstatus"), nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True)),
        sa.Column("triggered_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_reason", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(20, 8)),
        sa.Column("close_price", sa.Numeric(20, 8)),
        sa.Column("close_type", sa.String(length=20)),
        sa.Column("approved_by", sa.String(length=20), nullable=False, server_default="AUTO_MODE"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("symbol", sa.String(length=50)),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id")),
        sa.Column("signal_id", sa.Integer(), sa.ForeignKey("signals.id")),
        sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("level", sa.Enum("INFO", "WARNING", "ERROR", name="auditlevel"), nullable=False),
        sa.Column("message", sa.Text()),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("orders")
    op.drop_table("signals")
    op.drop_table("scan_cycles")
    op.drop_table("settings")
    op.drop_table("api_credentials")
