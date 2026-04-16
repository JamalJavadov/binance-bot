"""add scan detail persistence"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260328_0002"
down_revision = "20260328_0001"
branch_labels = None
depends_on = None

signal_direction_enum = postgresql.ENUM("LONG", "SHORT", name="signaldirection", create_type=False)
scan_symbol_outcome_enum = postgresql.ENUM(
    "UNSUPPORTED",
    "NO_SETUP",
    "FILTERED_OUT",
    "CANDIDATE",
    "QUALIFIED",
    "FAILED",
    name="scansymboloutcome",
    create_type=False,
)


def upgrade() -> None:
    scan_symbol_outcome_enum.create(op.get_bind(), checkfirst=True)
    op.add_column("audit_log", sa.Column("scan_cycle_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_audit_log_scan_cycle_id_scan_cycles",
        "audit_log",
        "scan_cycles",
        ["scan_cycle_id"],
        ["id"],
    )
    op.create_table(
        "scan_symbol_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scan_cycle_id", sa.Integer(), sa.ForeignKey("scan_cycles.id"), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("direction", signal_direction_enum, nullable=True),
        sa.Column("outcome", scan_symbol_outcome_enum, nullable=False),
        sa.Column("confirmation_score", sa.Integer(), nullable=True),
        sa.Column("final_score", sa.Integer(), nullable=True),
        sa.Column("score_breakdown", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("filter_reasons", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_scan_symbol_results_scan_cycle_id", "scan_symbol_results", ["scan_cycle_id"])


def downgrade() -> None:
    op.drop_index("ix_scan_symbol_results_scan_cycle_id", table_name="scan_symbol_results")
    op.drop_table("scan_symbol_results")
    op.drop_constraint("fk_audit_log_scan_cycle_id_scan_cycles", "audit_log", type_="foreignkey")
    op.drop_column("audit_log", "scan_cycle_id")
    scan_symbol_outcome_enum.drop(op.get_bind(), checkfirst=True)
