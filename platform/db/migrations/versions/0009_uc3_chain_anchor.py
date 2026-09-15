"""An out-of-chain anchor per chained table (PRD E8).

A hash chain proves its own internal consistency and nothing else. Truncate the
tail -- `delete from analysis_runs where seq > 400`, or empty the table
altogether -- and what is left is a perfectly valid chain: every `prev_hash`
still matches, every `row_hash` still recomputes, and the nightly verification
reports clean. Only deleting the *head* breaks anything, which is the one thing
a tamperer has no reason to do.

So the evidence that rows are missing from the end cannot live inside the chain.
`audit_chain_anchors` holds, per chained table, the head hash and the row count
as of the last verification that passed, plus when that was. Verification then
also asks two questions the walk cannot: is the chain shorter than it was, and
is the head it last ended on still in it. A truncation fails the first; a
re-chained tail fails the second.

This table is deliberately NOT part of the chain. Chaining it would put the
evidence back inside the thing it is evidence about.

**Grant requirement, owned by 0008.** `uc3_app` must have no INSERT, UPDATE or
DELETE on `audit_chain_anchors`; only the verification job's role writes it. A
public schema in Postgres 16 grants nothing to PUBLIC by default, so the table
is closed unless someone opens it -- but 0008 is the migration that curates
this role's grants, and an explicit `revoke all on table audit_chain_anchors
from uc3_app;` belongs there next to the other REVOKEs, so a future audit of
that file sees the full picture in one place. Not added here: 0008 is shipped
and is another change's to amend.

Honest limit, recorded so nobody reads more into this than it carries: the
anchor sits in the same database as the chain. A superuser who can rewrite the
tail can also rewrite this row, and re-chaining 10k rows takes under a second.
This detects truncation by anything short of that. Tamper-evidence against a
database superuser needs an anchor they cannot recompute -- a signed daily head
held off-host, or a WORM object -- which is deployment scope, not schema.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_uc3_chain_anchor"
down_revision = "0008_uc3_audit_role"
branch_labels = None
depends_on = None

TABLE = "audit_chain_anchors"


def upgrade() -> None:
    op.create_table(
        TABLE,
        # One row per chained table, keyed by its name: the anchor moves
        # forward, it is not a log. The chain is the log.
        sa.Column("table_name", sa.String(64), primary_key=True),
        sa.Column("head_hash", sa.String(64), nullable=False),
        # `row_count`, not `rows`: ROWS is a keyword in the window-frame
        # grammar and not worth the quoting it would need.
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("row_count >= 0", name="ck_audit_chain_anchors_row_count"),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
