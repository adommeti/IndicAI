"""Record which arm an attempt belongs to, and how long it took.

PRD D6's `quiz_attempts` stores a score; PRD D10 asks for "pass rate by language
vs an English-only control" and "time-on-task". Neither is recoverable after the
fact -- cohort assignment is deterministic but the *delivered* language is what
the employee actually saw, and elapsed time exists only while the attempt is
open -- so both are columns, written when the attempt is recorded.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_uc2_quiz_attempt_cohort"
down_revision = "0004_uc2_artifact_meta"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quiz_attempts",
        sa.Column("cohort", sa.String(16), nullable=False, server_default="native"),
    )
    op.add_column(
        "quiz_attempts",
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "quiz_attempts",
        sa.Column("pilot_id", sa.String(64), nullable=False, server_default="unassigned"),
    )
    op.create_check_constraint(
        "ck_quiz_attempts_cohort", "quiz_attempts", "cohort in ('native','control')"
    )
    op.create_check_constraint(
        "ck_quiz_attempts_score", "quiz_attempts", "score >= 0 and score <= max_score"
    )
    op.create_index("ix_quiz_attempts_report", "quiz_attempts", ["pilot_id", "language", "cohort"])


def downgrade() -> None:
    op.drop_index("ix_quiz_attempts_report", table_name="quiz_attempts")
    op.drop_constraint("ck_quiz_attempts_score", "quiz_attempts")
    op.drop_constraint("ck_quiz_attempts_cohort", "quiz_attempts")
    op.drop_column("quiz_attempts", "pilot_id")
    op.drop_column("quiz_attempts", "duration_ms")
    op.drop_column("quiz_attempts", "cohort")
