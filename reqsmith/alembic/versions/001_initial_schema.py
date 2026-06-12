"""Initial schema — all tables + append-only triggers on audit/outreach (Postgres).

Revision ID: 001
Revises:
Create Date: 2026-06-12
"""

from alembic import op

from reqsmith.persistence.models import Base

revision = "001"
down_revision = None
branch_labels = None
depends_on = None

APPEND_ONLY_TABLES = ("audit_events", "outreach_events")

TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)
    if bind.dialect.name == "postgresql":
        op.execute(TRIGGER_SQL)
        for table in APPEND_ONLY_TABLES:
            op.execute(
                f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION reject_mutation();"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};")
        op.execute("DROP FUNCTION IF EXISTS reject_mutation();")
    Base.metadata.drop_all(bind)
