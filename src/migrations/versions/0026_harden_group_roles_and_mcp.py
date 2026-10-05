"""Enforce expiring owner-issued MCP tokens."""

from alembic import op


revision = "0026_harden_group_roles_and_mcp"
down_revision = "0025_group_integrations_mcp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy unbounded tokens cannot be safely assigned a new lifetime, so revoke
    # them and set their expiry in the past before making the column mandatory.
    op.execute("UPDATE mcp_access_tokens SET revoked_at = now(), expires_at = created_at WHERE expires_at IS NULL AND revoked_at IS NULL")
    op.execute("UPDATE mcp_access_tokens SET expires_at = created_at WHERE expires_at IS NULL")
    op.execute("ALTER TABLE mcp_access_tokens ALTER COLUMN expires_at SET NOT NULL")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION resolve_manager_mcp_token(p_token_hash text)
        RETURNS TABLE(owner_id uuid, group_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
          SELECT t.owner_id, t.group_id
          FROM mcp_access_tokens AS t
          JOIN group_members AS m
            ON m.group_id = t.group_id
           AND m.owner_id = t.owner_id
           AND m.role = 'group_owner'
          WHERE t.token_hash = p_token_hash
            AND t.revoked_at IS NULL
            AND t.expires_at > now();
        $$;
        REVOKE ALL ON FUNCTION resolve_manager_mcp_token(text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION resolve_manager_mcp_token(text) TO app_user;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE mcp_access_tokens ALTER COLUMN expires_at DROP NOT NULL")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION resolve_manager_mcp_token(p_token_hash text)
        RETURNS TABLE(owner_id uuid, group_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
          SELECT t.owner_id, t.group_id
          FROM mcp_access_tokens AS t
          WHERE t.token_hash = p_token_hash
            AND t.revoked_at IS NULL
            AND (t.expires_at IS NULL OR t.expires_at > now());
        $$;
        REVOKE ALL ON FUNCTION resolve_manager_mcp_token(text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION resolve_manager_mcp_token(text) TO app_user;
        """
    )
