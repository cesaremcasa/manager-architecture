"""Create the tenant foundation and fail-closed RLS policies."""

from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("""
    DO $$ BEGIN
      CREATE ROLE app_user LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$;
    """)
    op.execute("""
    CREATE TABLE restaurants (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      name text NOT NULL,
      currency char(3) NOT NULL DEFAULT 'USD',
      timezone text NOT NULL DEFAULT 'America/New_York',
      business_day_cutoff time NOT NULL DEFAULT '04:00',
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE owners (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id uuid NOT NULL UNIQUE,
      tenant_id uuid NOT NULL REFERENCES restaurants(id),
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE manager_inboxes (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL UNIQUE REFERENCES restaurants(id),
      slug citext NOT NULL UNIQUE,
      token_hash text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE inbox_items (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL REFERENCES restaurants(id),
      message_id text NOT NULL,
      sender text NOT NULL,
      subject text NOT NULL DEFAULT '',
      status text NOT NULL DEFAULT 'received',
      raw_ref text,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, message_id)
    );
    """)
    for table in ("restaurants", "owners", "manager_inboxes", "inbox_items"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute("GRANT USAGE ON SCHEMA public TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO app_user")
    op.execute("""
    CREATE POLICY tenant_restaurants ON restaurants FOR ALL TO app_user
      USING (id = current_setting('app.tenant_id', true)::uuid)
      WITH CHECK (id = current_setting('app.tenant_id', true)::uuid);
    CREATE POLICY tenant_owners ON owners FOR ALL TO app_user
      USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
    CREATE POLICY tenant_inboxes ON manager_inboxes FOR ALL TO app_user
      USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
    CREATE POLICY tenant_inbox_items ON inbox_items FOR ALL TO app_user
      USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS inbox_items, manager_inboxes, owners, restaurants CASCADE")
