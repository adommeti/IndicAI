"""A GIN index on `analysis_runs.output`, which three containment queries assume.

`metrics.qa_sample_statement` already documents its predicate as written for one --
"the `@>` containment is on the whole `output` document so a GIN index on that JSONB
column can serve it" -- and there was none. Two more containment queries have since
joined it: `metrics.demo_free`, which every precision and QA read applies, and
`metrics.demo_row_counts`, which runs two `count(*) ... @>` on a route the
least-privileged role can refresh at will.

Without the index each of those is a sequential scan of `analysis_runs`, the table
designed only to grow. `chain_status` refuses unbounded per-request work on exactly this
reasoning (`api.py`); an aggregate that quietly does it instead is the same defect in a
less obvious place.

`jsonb_ops`, the default, rather than `jsonb_path_ops`: the path variant is smaller and
faster for `@>` alone, but it cannot serve key-existence (`?`) at all, and the three
predicates here are not the last ones this column will carry. The difference is
milliseconds at this table's size; the difference in what the index can answer is
permanent.

Created `CONCURRENTLY` is deliberately NOT used. It cannot run inside a transaction,
and alembic runs migrations in one; a build of this index on a POC-sized table is a
sub-second lock, while the machinery to run it outside the transaction is a standing
hazard for whoever writes the next migration.
"""

from alembic import op

revision = "0014_uc3_analysis_output_gin"
down_revision = "0013_uc3_disposition_idempotency"
branch_labels = None
depends_on = None

INDEX = "ix_analysis_runs_output_gin"


def upgrade() -> None:
    op.create_index(
        INDEX,
        "analysis_runs",
        ["output"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="analysis_runs")
