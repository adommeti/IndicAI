"""A retried disposition must not become a second permanent ruling (uc3/P7).

`POST /flags/{id}/dispositions` writes into an append-only, hash-chained table whose
rows the application's database role can never update or delete. That makes a duplicate
write unusually expensive: an ordinary duplicate is a row somebody cleans up, and this
one is a second compliance ruling on the same flag, by the same reviewer, that nobody
can withdraw. The only guard before this migration was the UI disabling its own button
while the request was in flight (`DispositionForm.tsx`), which a page refresh, a proxy
retry or a dropped connection all defeat.

`docs/build/BLOCKERS.md` recorded it against uc3/P6 and pointed it here:
".claude/rules/apps.md wants side effects idempotent per a durable key and safe to
retry ... belongs with uc3/P7's review of the write paths."

## Why the reviewer is part of the key

`(flag_id, idempotency_key)` was the obvious shape and it is wrong. Nothing in the
contract stops two reviewers choosing the same key on the same flag -- a deterministic
key, a per-batch key, a client that seeds from the flag id -- and under that constraint
the second reviewer's ruling would not be recorded at all: they would receive 200 and
the *first* reviewer's receipt, having had their own decision silently discarded on a
table nobody can correct afterwards. A retry must be safe; a collision between two
intents must not be mistaken for one.

Including `reviewer_id` makes the key mean what the route needs it to mean: "this
reviewer's decision on this flag, sent once". Two reviewers cannot collide, and a
reviewer who reuses a key with a different decision gets a 409 rather than a silent
replay (`apps/comms_surveillance/api.py`).

## Why nullable, and why the uniqueness is partial by consequence

The column is nullable so this migration is additive: rows written before it exist, and
a caller that sends no key keeps working. PostgreSQL treats NULLs as distinct in a
unique index, so unkeyed writes are unconstrained exactly as they are today, while any
two writes that DO carry the same key for the same flag collide at the database rather
than at a check the request could race past.

## Why the key is not added to the hash

`apps/comms_surveillance/audit.py` pins what each chain hashes in `HASHED_COLUMNS`
precisely so "adding a column is inert" -- deriving the list from the live table meant
any ALTER TABLE retroactively rewrote the payload of every historical row and broke all
three chains at row 1. This column therefore stays out of the hash, and the schema
version is not bumped, because bumping it "invalidates stored hashes deliberately and
loudly" and an idempotency token does not deserve that.

Nothing is lost by leaving it out. The hash covers the reviewer's decision -- the flag,
the verdict, the note and the identity that signed it. The key is a de-duplication
token about the *delivery* of that decision, not part of it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_uc3_disposition_idempotency"
down_revision = "0012_uc1_retention_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dispositions",
        sa.Column("idempotency_key", sa.String(128), nullable=True),
    )
    op.create_unique_constraint(
        "uq_dispositions_flag_idempotency_key",
        "dispositions",
        ["flag_id", "reviewer_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_dispositions_flag_idempotency_key", "dispositions", type_="unique")
    op.drop_column("dispositions", "idempotency_key")
