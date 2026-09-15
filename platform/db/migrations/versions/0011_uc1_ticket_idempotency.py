"""One helpdesk ticket per turn, enforced by the database (uc1/P4).

The thing this migration exists for is `uq_ticket_filings_turn`. Filing a ticket
is visible to the employee and to the IT queue, so a retry that files a second
one is a defect nobody can undo from inside the application. The retries are
real: a voice client reconnecting mid-turn, a Celery redelivery after a worker
was lost, an operator replaying a stuck session. Guarding that with a SELECT
("has this turn filed yet?") followed by an INSERT loses exactly the race it is
meant to cover -- both callers read nothing, both file. A unique constraint does
not lose it: the second INSERT blocks on the first, and when the first commits
the second is refused. The refusal is the idempotent path, and the row the
winner wrote is what the loser reads back.

`ck_ticket_filings_number` is the smaller, blunter rule: a row may hold a ticket
number only when its status is `filed`. A pending filing has no number because
Zammad has not issued one, and the one failure mode worse than not filing a
ticket is telling an employee a number that does not exist. The database refuses
to store that combination at all, so no code path can invent it.

Zammad itself is not in the core stack; it runs under the `ticketing` compose
profile. This table is Postgres-side only and upgrades with everything else.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011_uc1_ticket_idempotency"
down_revision = "0010_uc3_seq_generated_always"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_filings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.String(128), nullable=False),
        sa.Column("source_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("ticket_number", sa.String(64), nullable=True),
        sa.Column("ticket_id", sa.BigInteger(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("session_id", "turn_index", name="uq_ticket_filings_turn"),
        sa.CheckConstraint(
            "status in ('filing','filed','pending')", name="ck_ticket_filings_status"
        ),
        sa.CheckConstraint("turn_index >= 0", name="ck_ticket_filings_turn_index"),
        sa.CheckConstraint(
            "(status = 'filed') = (ticket_number is not null)", name="ck_ticket_filings_number"
        ),
    )


def downgrade() -> None:
    op.drop_table("ticket_filings")
