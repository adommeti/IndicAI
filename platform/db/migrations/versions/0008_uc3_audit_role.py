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

DEPLOYMENT PRECONDITION -- the login role the application connects as MUST NOT
OWN `analysis_runs`, `flags` or `dispositions`, and must hold no direct grants
on them beyond its membership of `uc3_app`. A table owner carries UPDATE and
DELETE implicitly and can re-grant them to itself at will, so a deployment
where the app's login role is also the migration/owner role has no control here
at all, whatever this migration says. Own the tables with a separate migration
role; give the application only `uc3_app`.
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
READ_WRITE = ("calls", "transcript_segments")


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
    # Without this the role can reach no table at all, whatever the table
    # grants say. Postgres 15+ still grants schema usage to PUBLIC by default,
    # so this is usually redundant -- but a hardened cluster revokes it, and a
    # privilege the migration relies on is one the migration should establish
    # rather than inherit from the installation's defaults.
    op.execute(f"grant usage on schema public to {ROLE};")

    for table in CHAINED:
        # Revoke first and unconditionally: a role that already existed may
        # carry grants from an earlier deploy, and the point of this migration
        # is the *absence* of update and delete, not the presence of insert.
        op.execute(f"revoke all on table {table} from {ROLE};")
        op.execute(f"grant {APPEND_ONLY_GRANTS} on table {table} to {ROLE};")
        # Grants are additive, so constraining one role proves nothing while
        # write access is reachable by another path. PUBLIC is the path that
        # costs nothing to close, and closing it is what makes "cannot UPDATE"
        # a property of the table rather than of one role's grant list.
        op.execute(f"revoke update, delete on table {table} from public;")

    # `seq` is an identity column. Unlike `serial`, an identity column's
    # implicit sequence is advanced under the table's INSERT privilege and
    # needs no separate grant -- hence no sequence grant here. The blanket
    # `on all sequences in schema public` this migration used to emit was both
    # unnecessary and far too wide: it handed the role every other app's
    # sequences too.

    # Everything else the app touches stays read-write.
    for table in READ_WRITE:
        op.execute(f"grant select, insert, update, delete on table {table} to {ROLE};")

    # A table added later (0009_uc3_chain_anchor and anything after it) would
    # otherwise land with whatever PUBLIC happens to hold and be governed by
    # nothing until someone remembers to write the revoke. Default privileges
    # make the safe state the default one.
    #
    # Two limits worth knowing rather than discovering: this binds only to
    # tables created by the role that runs this statement (the migration role),
    # and it governs PUBLIC only -- a new audit table still needs its own
    # explicit `grant select, insert ... to uc3_app` in the revision that
    # creates it.
    op.execute(
        "alter default privileges in schema public revoke update, delete on tables from public;"
    )


def downgrade() -> None:
    for table in (*CHAINED, *READ_WRITE):
        op.execute(f"revoke all on table {table} from {ROLE};")
    op.execute(f"revoke usage on schema public from {ROLE};")
    # Deliberately asymmetric: the revokes against PUBLIC are not undone.
    # Reversing them means executing `grant update, delete ... to public`, and
    # a downgrade that widens write access to every role in the cluster is a
    # worse outcome than a downgrade that leaves a tightening in place. The
    # default-privileges entry is a no-op to leave behind in any case -- it
    # only restates what Postgres already does for a newly created table.
    #
    # The role itself is left in place: it may own grants elsewhere, and
    # dropping a role another database object depends on fails noisily.
