"""A DB role for the app with INSERT and SELECT only on the chained tables (E8).

The hash chain makes tampering *detectable*. This makes it require a second
credential: the role the application connects as has no UPDATE and no DELETE on
`analysis_runs`, `flags` or `dispositions`, so an application-level bug, a SQL
injection, or a compromised app credential cannot rewrite history at all --
they can only append, which the chain then binds.

Idempotent by construction, because it has to be safe on a database where the
role already exists (a re-deploy, a restored snapshot, a shared cluster):
`CREATE ROLE` is guarded by a catalogue lookup, and every GRANT/REVOKE is
naturally repeatable.

The password is **not** set here. Migrations are in git; a credential in one
would be a secret in the repository. The role is created `NOLOGIN` and the
deploy grants it to whichever login role the environment already provisioned --
see `docs/build/RUNBOOK.md`.
"""

from alembic import op

revision = "0008_uc3_audit_role"
down_revision = "0007_uc3_audit_chain"
branch_labels = None
depends_on = None

ROLE = "uc3_app"
CHAINED = ("analysis_runs", "flags", "dispositions")
# The app still needs ordinary access to the rest of its schema; only the
# audit tables are append-only.
APPEND_ONLY_GRANTS = "select, insert"


def upgrade() -> None:
    op.execute(
        f"""
        do $$
        begin
            if not exists (select 1 from pg_roles where rolname = '{ROLE}') then
                create role {ROLE} nologin;
            end if;
        end
        $$;
        """
    )
    for table in CHAINED:
        # Revoke first and unconditionally: a role that already existed may
        # carry grants from an earlier deploy, and the point of this migration
        # is the *absence* of update and delete, not the presence of insert.
        op.execute(f"revoke all on table {table} from {ROLE};")
        op.execute(f"grant {APPEND_ONLY_GRANTS} on table {table} to {ROLE};")
        # `seq` is an identity column, so inserting needs the sequence.
        op.execute(f"grant usage, select on all sequences in schema public to {ROLE};")

    # Everything else the app touches stays read-write.
    for table in ("calls", "transcript_segments"):
        op.execute(f"grant select, insert, update, delete on table {table} to {ROLE};")


def downgrade() -> None:
    for table in (*CHAINED, "calls", "transcript_segments"):
        op.execute(f"revoke all on table {table} from {ROLE};")
    # The role itself is left in place: it may own grants elsewhere, and
    # dropping a role another database object depends on fails noisily.
