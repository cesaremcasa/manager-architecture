"""Add restaurant groups, governed integrations, lineage, and MCP access.

Group records use a separate transaction setting (``app.group_id``).  The API
first resolves ownership through security-definer functions, then opens this
scoped transaction.  Unit tables and their existing ``app.tenant_id`` RLS
policies remain unchanged.
"""

from alembic import op


revision = "0025_group_integrations_mcp"
down_revision = "0024_owner_session_active_tenant"
branch_labels = None
depends_on = None


GROUP_TABLES = (
    "restaurant_groups",
    "group_units",
    "group_members",
    "integration_connections",
    "oauth_authorization_states",
    "import_runs",
    "import_errors",
    "integration_records",
    "record_lineage",
    "mcp_access_tokens",
    "action_proposals",
    "invoice_approval_events",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE restaurant_groups (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          name text NOT NULL,
          operating_concept text NOT NULL DEFAULT 'restaurant_group',
          timezone text NOT NULL DEFAULT 'America/New_York',
          currency char(3) NOT NULL DEFAULT 'USD',
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE group_units (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
          tenant_id uuid NOT NULL UNIQUE REFERENCES restaurants(id) ON DELETE RESTRICT,
          unit_code text NOT NULL,
          display_name text NOT NULL,
          seats integer NOT NULL CHECK (seats > 0),
          city text NOT NULL,
          state text NOT NULL,
          latitude numeric(9,6),
          longitude numeric(9,6),
          active boolean NOT NULL DEFAULT true,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (group_id, unit_code)
        );
        CREATE TABLE group_members (
          group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
          owner_id uuid NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
          role text NOT NULL DEFAULT 'group_owner'
            CHECK (role IN ('group_owner', 'operator', 'viewer')),
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (group_id, owner_id)
        );

        CREATE TABLE integration_connections (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
          owner_id uuid NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
          provider text NOT NULL CHECK (provider IN ('google_sheets', 'open_meteo', 'yelp', 'demo_workbook')),
          status text NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'connected', 'ready', 'disabled', 'error')),
          display_name text,
          external_file_id text,
          external_file_name text,
          source_revision text,
          scopes text[] NOT NULL DEFAULT '{}',
          access_token_ciphertext text,
          refresh_token_ciphertext text,
          access_token_expires_at timestamptz,
          config jsonb NOT NULL DEFAULT '{}'::jsonb,
          last_synced_at timestamptz,
          last_validated_at timestamptz,
          last_error_code text,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (group_id, provider)
        );
        CREATE TABLE oauth_authorization_states (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
          owner_id uuid NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
          provider text NOT NULL,
          state_hash text NOT NULL UNIQUE,
          code_verifier_ciphertext text NOT NULL,
          expires_at timestamptz NOT NULL,
          consumed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE import_runs (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
          connection_id uuid REFERENCES integration_connections(id) ON DELETE SET NULL,
          status text NOT NULL DEFAULT 'validating'
            CHECK (status IN ('validating', 'failed', 'published', 'superseded')),
          source_revision text,
          schema_version text NOT NULL,
          source_hash text,
          started_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz,
          published_at timestamptz,
          record_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
          validation_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE NULLS NOT DISTINCT (connection_id, source_revision, schema_version)
        );
        CREATE TABLE import_errors (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
          import_run_id uuid NOT NULL REFERENCES import_runs(id) ON DELETE CASCADE,
          sheet_name text,
          row_number integer,
          field_name text,
          code text NOT NULL,
          message text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE integration_records (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
          tenant_id uuid REFERENCES restaurants(id) ON DELETE RESTRICT,
          connection_id uuid REFERENCES integration_connections(id) ON DELETE SET NULL,
          import_run_id uuid NOT NULL REFERENCES import_runs(id) ON DELETE RESTRICT,
          dataset text NOT NULL,
          external_id text NOT NULL,
          effective_at timestamptz,
          source_type text NOT NULL CHECK (source_type IN ('synthetic_scenario', 'public_benchmark', 'public_api', 'customer_source')),
          schema_version text NOT NULL,
          workflow_status text NOT NULL DEFAULT 'received'
            CHECK (workflow_status IN ('received', 'pending_approval', 'approved', 'rejected')),
          payload jsonb NOT NULL,
          payload_hash text NOT NULL,
          source_updated_at timestamptz,
          ingested_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (connection_id, dataset, external_id)
        );
        CREATE INDEX integration_records_group_dataset_time
          ON integration_records (group_id, dataset, effective_at DESC);
        CREATE INDEX integration_records_tenant_dataset_time
          ON integration_records (tenant_id, dataset, effective_at DESC);
        CREATE TABLE record_lineage (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
          record_id uuid NOT NULL REFERENCES integration_records(id) ON DELETE CASCADE,
          import_run_id uuid NOT NULL REFERENCES import_runs(id) ON DELETE RESTRICT,
          source_file_id text,
          source_file_name text,
          source_sheet text,
          source_row integer,
          source_revision text,
          source_url text,
          source_hash text NOT NULL,
          imported_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (record_id, import_run_id)
        );
        CREATE INDEX record_lineage_group_record ON record_lineage (group_id, record_id, imported_at DESC);

        CREATE TABLE mcp_access_tokens (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
          owner_id uuid NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
          label text NOT NULL,
          token_hash text NOT NULL UNIQUE,
          expires_at timestamptz,
          revoked_at timestamptz,
          last_used_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE action_proposals (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
          tenant_id uuid NOT NULL REFERENCES restaurants(id) ON DELETE RESTRICT,
          kind text NOT NULL CHECK (kind IN ('task', 'shift', 'production_plan', 'inventory_count', 'invoice_approval')),
          status text NOT NULL DEFAULT 'proposed'
            CHECK (status IN ('proposed', 'approved', 'rejected', 'cancelled')),
          created_by text NOT NULL DEFAULT 'manager_ai',
          payload jsonb NOT NULL,
          source_record_id uuid REFERENCES integration_records(id) ON DELETE SET NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          resolved_at timestamptz,
          resolved_by_owner_id uuid REFERENCES owners(id),
          resolution_reason text
        );
        CREATE TABLE invoice_approval_events (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
          tenant_id uuid NOT NULL REFERENCES restaurants(id) ON DELETE RESTRICT,
          proposal_id uuid NOT NULL UNIQUE REFERENCES action_proposals(id) ON DELETE RESTRICT,
          source_record_id uuid NOT NULL REFERENCES integration_records(id) ON DELETE RESTRICT,
          import_run_id uuid NOT NULL REFERENCES import_runs(id) ON DELETE RESTRICT,
          owner_id uuid NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
          reason text NOT NULL,
          operational_effect jsonb NOT NULL DEFAULT '{}'::jsonb,
          approved_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    for table in GROUP_TABLES:
        group_column = "id" if table == "restaurant_groups" else "group_id"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO app_user")
        op.execute(
            f"CREATE POLICY {table}_group ON {table} FOR ALL TO app_user "
            f"USING ({group_column} = current_setting('app.group_id', true)::uuid) "
            f"WITH CHECK ({group_column} = current_setting('app.group_id', true)::uuid)"
        )

    op.execute(
        """
        CREATE FUNCTION list_owner_groups(p_owner_id uuid)
        RETURNS TABLE(group_id uuid, group_name text, operating_concept text, timezone text, currency char(3), role text)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
          SELECT g.id, g.name, g.operating_concept, g.timezone, g.currency, m.role
          FROM restaurant_groups AS g
          JOIN group_members AS m ON m.group_id = g.id
          WHERE m.owner_id = p_owner_id
          ORDER BY g.name;
        $$;
        CREATE FUNCTION list_authorized_group_units(p_owner_id uuid, p_group_id uuid)
        RETURNS TABLE(tenant_id uuid, unit_code text, display_name text, seats integer, city text, state text,
                      latitude numeric, longitude numeric, member_role text)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
          SELECT gu.tenant_id, gu.unit_code, gu.display_name, gu.seats, gu.city, gu.state,
                 gu.latitude, gu.longitude, gm.role
          FROM group_members AS gm
          JOIN group_units AS gu ON gu.group_id = gm.group_id AND gu.active = true
          WHERE gm.owner_id = p_owner_id AND gm.group_id = p_group_id
          ORDER BY gu.unit_code;
        $$;
        CREATE FUNCTION resolve_manager_mcp_token(p_token_hash text)
        RETURNS TABLE(owner_id uuid, group_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
          SELECT t.owner_id, t.group_id
          FROM mcp_access_tokens AS t
          WHERE t.token_hash = p_token_hash
            AND t.revoked_at IS NULL
            AND (t.expires_at IS NULL OR t.expires_at > now());
        $$;
        CREATE FUNCTION consume_oauth_authorization_state(p_state_hash text)
        RETURNS TABLE(group_id uuid, owner_id uuid, provider text, code_verifier_ciphertext text)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        BEGIN
          RETURN QUERY
          UPDATE oauth_authorization_states AS s
          SET consumed_at = now()
          WHERE s.state_hash = p_state_hash
            AND s.consumed_at IS NULL
            AND s.expires_at > now()
          RETURNING s.group_id, s.owner_id, s.provider, s.code_verifier_ciphertext;
        END;
        $$;
        CREATE FUNCTION mark_manager_mcp_token_used(p_token_hash text)
        RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
          UPDATE mcp_access_tokens SET last_used_at = now()
          WHERE token_hash = p_token_hash AND revoked_at IS NULL;
        $$;
        """
    )
    for function in (
        "list_owner_groups(uuid)",
        "list_authorized_group_units(uuid,uuid)",
        "resolve_manager_mcp_token(text)",
        "consume_oauth_authorization_state(text)",
        "mark_manager_mcp_token_used(text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO app_user")


def downgrade() -> None:
    for function in (
        "resolve_manager_mcp_token(text)",
        "consume_oauth_authorization_state(text)",
        "mark_manager_mcp_token_used(text)",
        "list_authorized_group_units(uuid,uuid)",
        "list_owner_groups(uuid)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
    op.execute(
        "DROP TABLE IF EXISTS invoice_approval_events, action_proposals, mcp_access_tokens, "
        "record_lineage, integration_records, import_errors, import_runs, oauth_authorization_states, "
        "integration_connections, group_members, group_units, restaurant_groups CASCADE"
    )
