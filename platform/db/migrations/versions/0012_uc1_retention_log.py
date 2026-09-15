"""The audit record for PRD C8's deletions (uc1/P7).

C8 requires audio deleted after 30 days and transcripts after 90, "deleted by a
scheduled job; deletions logged". The logging half is the half that can be
audited: a deletion leaves no trace by construction, so if the job does not
write down that it ran, a reviewer a year later cannot tell a retention policy
that worked from one whose beat schedule was never deployed. Both look like an
empty result set.

So this table is the evidence, and its constraints are what make the evidence
worth reading. `ck_retention_deletions_dry_run` refuses a dry run that claims
deletions -- the rehearsal mode must never be mistakable for the real pass.
`ck_retention_deletions_window_scope` ties the covered time window to the match
count, so a row cannot report "deleted 900 rows" with no window to say which
900. `ck_retention_deletions_counts` keeps `rows_deleted` inside `rows_matched`.

Deliberately not stored: any identifier or content of what was deleted. An audit
row that quoted the transcript it deleted would keep the data the policy exists
to remove.

Append-only in practice but not by grant: the uc3 chain tables carry
`prev_hash`/`row_hash` because a compliance reviewer's decisions are adversarial
evidence. These rows are operational, written by one scheduled job, and the
value here is the record itself; adding a hash chain would imply a tamper model
the rest of the uc1 schema does not have.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_uc1_retention_log"
down_revision = "0011_uc1_ticket_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "retention_deletions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_run_id", sa.Uuid(), nullable=False),
        sa.Column("app", sa.String(64), nullable=False),
        sa.Column("policy", sa.String(64), nullable=False),
        sa.Column("target", sa.String(200), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rows_matched", sa.BigInteger(), nullable=False),
        sa.Column("rows_deleted", sa.BigInteger(), nullable=False),
        sa.Column("batches", sa.Integer(), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detail", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('completed','failed')", name="ck_retention_deletions_status"
        ),
        sa.CheckConstraint("retention_days > 0", name="ck_retention_deletions_days"),
        sa.CheckConstraint(
            "batch_size > 0 and batches >= 0", name="ck_retention_deletions_batching"
        ),
        sa.CheckConstraint(
            "rows_matched >= 0 and rows_deleted >= 0 and rows_deleted <= rows_matched",
            name="ck_retention_deletions_counts",
        ),
        sa.CheckConstraint(
            "not dry_run or rows_deleted = 0", name="ck_retention_deletions_dry_run"
        ),
        sa.CheckConstraint(
            "(window_start is null) = (window_end is null)",
            name="ck_retention_deletions_window_pair",
        ),
        sa.CheckConstraint(
            "(rows_matched = 0) = (window_start is null)",
            name="ck_retention_deletions_window_scope",
        ),
        sa.CheckConstraint(
            "window_end is null or window_end >= window_start",
            name="ck_retention_deletions_window_order",
        ),
        sa.CheckConstraint("finished_at >= started_at", name="ck_retention_deletions_duration"),
    )
    op.create_index(
        "ix_retention_deletions_policy_finished", "retention_deletions", ["policy", "finished_at"]
    )
    op.create_index("ix_retention_deletions_job_run", "retention_deletions", ["job_run_id"])


def downgrade() -> None:
    op.drop_index("ix_retention_deletions_job_run", table_name="retention_deletions")
    op.drop_index("ix_retention_deletions_policy_finished", table_name="retention_deletions")
    op.drop_table("retention_deletions")
