"""Training-localizer tables from PRD D6: modules, segments, localizations, quiz."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_uc2_modules"
down_revision = "0002_helpdesk_turns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "modules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_lang", sa.String(16), nullable=False, server_default="en-IN"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "segments",
        sa.Column("module_id", sa.Uuid(), sa.ForeignKey("modules.id"), primary_key=True),
        sa.Column("seg_id", sa.Integer(), primary_key=True),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("end_ms > start_ms", name="ck_segments_duration_positive"),
    )
    # (module_id, seg_id, language, stage, version) — a stage re-run appends a
    # version instead of overwriting, which is what makes stages independently
    # re-runnable (D5).
    op.create_table(
        "localizations",
        sa.Column("module_id", sa.Uuid(), sa.ForeignKey("modules.id"), primary_key=True),
        sa.Column("seg_id", sa.Integer(), primary_key=True),
        sa.Column("language", sa.String(16), primary_key=True),
        sa.Column("stage", sa.String(32), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("meta", JSONB(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("version > 0", name="ck_localizations_version_positive"),
        sa.CheckConstraint(
            "stage in ('adapt','translate','post_edit','backtranslate','approved')",
            name="ck_localizations_stage",
        ),
    )
    op.create_index(
        "ix_localizations_module_language", "localizations", ["module_id", "language", "stage"]
    )
    op.create_table(
        "quiz_items",
        sa.Column("module_id", sa.Uuid(), sa.ForeignKey("modules.id"), primary_key=True),
        sa.Column("language", sa.String(16), primary_key=True),
        sa.Column("item_id", sa.Integer(), primary_key=True),
        sa.Column("seg_id", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("options", JSONB(), nullable=False),
        sa.Column("answer", sa.Integer(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "artifacts",
        sa.Column("module_id", sa.Uuid(), sa.ForeignKey("modules.id"), primary_key=True),
        sa.Column("language", sa.String(16), primary_key=True),
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "quiz_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("module_id", sa.Uuid(), sa.ForeignKey("modules.id"), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("employee_id", sa.String(128), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("max_score", sa.Integer(), nullable=False),
        sa.Column(
            "taken_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_quiz_attempts_module_id", "quiz_attempts", ["module_id"])


def downgrade() -> None:
    op.drop_table("quiz_attempts")
    op.drop_table("artifacts")
    op.drop_table("quiz_items")
    op.drop_table("localizations")
    op.drop_table("segments")
    op.drop_table("modules")
