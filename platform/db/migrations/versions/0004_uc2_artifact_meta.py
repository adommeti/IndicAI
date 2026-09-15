"""Give artifacts a meta column so production can store what it measured.

PRD D6's `artifacts` table records uri, sha256 and timestamps but has nowhere to
put a measurement. uc2/P4 has to store the timing-fit it computed from the dub's
SRT export against the module-language it belongs to, and the natural home is
the artifact row for that dub. Adding a column rather than a table: the fact
belongs to the artifact and has no life of its own.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_uc2_artifact_meta"
down_revision = "0003_uc2_modules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column("meta", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("artifacts", "meta")
