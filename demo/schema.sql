-- Minimal disposable schema: policies/functions copied from Manager migrations
-- 0001_foundation.py and 0025_group_integrations_mcp.py.
DO $$ BEGIN
  CREATE ROLE app_user LOGIN PASSWORD 'demo_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
GRANT USAGE ON SCHEMA public TO app_user;

CREATE TABLE restaurants (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  currency char(3) NOT NULL DEFAULT 'USD',
  timezone text NOT NULL DEFAULT 'America/New_York',
  business_day_cutoff time NOT NULL DEFAULT '04:00',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE owners (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL UNIQUE,
  tenant_id uuid NOT NULL REFERENCES restaurants(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE inbox_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES restaurants(id),
  message_id text NOT NULL,
  sender text NOT NULL,
  subject text NOT NULL DEFAULT '',
  UNIQUE (tenant_id, message_id)
);
CREATE TABLE restaurant_groups (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  operating_concept text NOT NULL DEFAULT 'restaurant_group',
  timezone text NOT NULL DEFAULT 'America/New_York',
  currency char(3) NOT NULL DEFAULT 'USD'
);
CREATE TABLE group_units (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
  tenant_id uuid NOT NULL UNIQUE REFERENCES restaurants(id),
  unit_code text NOT NULL,
  display_name text NOT NULL,
  seats integer NOT NULL CHECK (seats > 0),
  city text NOT NULL,
  state text NOT NULL,
  latitude numeric(9,6),
  longitude numeric(9,6),
  active boolean NOT NULL DEFAULT true,
  UNIQUE (group_id, unit_code)
);
CREATE TABLE group_members (
  group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
  owner_id uuid NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
  role text NOT NULL DEFAULT 'group_owner' CHECK (role IN ('group_owner','operator','viewer')),
  PRIMARY KEY (group_id, owner_id)
);
CREATE TABLE import_runs (
  id uuid PRIMARY KEY,
  group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
  schema_version text NOT NULL,
  status text NOT NULL DEFAULT 'published'
);
CREATE TABLE integration_records (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id uuid NOT NULL REFERENCES restaurant_groups(id) ON DELETE CASCADE,
  tenant_id uuid REFERENCES restaurants(id),
  import_run_id uuid NOT NULL REFERENCES import_runs(id),
  dataset text NOT NULL,
  external_id text NOT NULL,
  effective_at timestamptz,
  source_type text NOT NULL CHECK (source_type IN ('synthetic_scenario','public_benchmark','public_api','customer_source')),
  schema_version text NOT NULL,
  workflow_status text NOT NULL DEFAULT 'received',
  payload jsonb NOT NULL,
  payload_hash text NOT NULL,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (group_id, dataset, external_id)
);
GRANT SELECT, INSERT, UPDATE ON restaurants, owners, inbox_items, restaurant_groups,
  group_units, group_members, import_runs, integration_records TO app_user;

ALTER TABLE restaurants ENABLE ROW LEVEL SECURITY;
ALTER TABLE restaurants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_restaurants ON restaurants FOR ALL TO app_user
  USING (id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (id = current_setting('app.tenant_id', true)::uuid);
ALTER TABLE owners ENABLE ROW LEVEL SECURITY;
ALTER TABLE owners FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_owners ON owners FOR ALL TO app_user
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
ALTER TABLE inbox_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE inbox_items FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_inbox_items ON inbox_items FOR ALL TO app_user
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

DO $$ DECLARE table_name text; group_column text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['restaurant_groups','group_units','group_members','import_runs','integration_records'] LOOP
    group_column := CASE WHEN table_name = 'restaurant_groups' THEN 'id' ELSE 'group_id' END;
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO app_user USING (%I = current_setting(''app.group_id'', true)::uuid) WITH CHECK (%I = current_setting(''app.group_id'', true)::uuid)', table_name || '_group', table_name, group_column, group_column);
  END LOOP;
END $$;

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
REVOKE ALL ON FUNCTION list_authorized_group_units(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_authorized_group_units(uuid,uuid) TO app_user;

INSERT INTO restaurants (id,name) VALUES
 ('10000000-0000-4000-8000-000000000001','Synthetic Cedar Unit'),
 ('10000000-0000-4000-8000-000000000002','Synthetic Birch Unit');
INSERT INTO owners (id,user_id,tenant_id) VALUES
 ('20000000-0000-4000-8000-000000000001','30000000-0000-4000-8000-000000000001','10000000-0000-4000-8000-000000000001'),
 ('20000000-0000-4000-8000-000000000002','30000000-0000-4000-8000-000000000002','10000000-0000-4000-8000-000000000002'),
 ('20000000-0000-4000-8000-000000000003','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000002');
INSERT INTO inbox_items (tenant_id,message_id,sender,subject) VALUES
 ('10000000-0000-4000-8000-000000000001','synthetic-cedar','demo@example.test','Synthetic Cedar receipt'),
 ('10000000-0000-4000-8000-000000000002','synthetic-birch','demo@example.test','Synthetic Birch receipt');
INSERT INTO restaurant_groups (id,name) VALUES
 ('40000000-0000-4000-8000-000000000001','Synthetic Cedar Group'),
 ('40000000-0000-4000-8000-000000000002','Synthetic Birch Group');
INSERT INTO group_units (group_id,tenant_id,unit_code,display_name,seats,city,state) VALUES
 ('40000000-0000-4000-8000-000000000001','10000000-0000-4000-8000-000000000001','CEDAR','Cedar Kitchen',34,'Albany','NY'),
 ('40000000-0000-4000-8000-000000000002','10000000-0000-4000-8000-000000000002','BIRCH','Birch Kitchen',28,'Troy','NY');
INSERT INTO group_members (group_id,owner_id,role) VALUES
 ('40000000-0000-4000-8000-000000000001','20000000-0000-4000-8000-000000000001','group_owner'),
 ('40000000-0000-4000-8000-000000000001','20000000-0000-4000-8000-000000000002','viewer'),
 ('40000000-0000-4000-8000-000000000002','20000000-0000-4000-8000-000000000003','operator');
INSERT INTO import_runs (id,group_id,schema_version) VALUES
 ('50000000-0000-4000-8000-000000000001','40000000-0000-4000-8000-000000000001','demo-v1'),
 ('50000000-0000-4000-8000-000000000002','40000000-0000-4000-8000-000000000002','demo-v1');
INSERT INTO integration_records (group_id,tenant_id,import_run_id,dataset,external_id,effective_at,source_type,schema_version,payload,payload_hash) VALUES
 ('40000000-0000-4000-8000-000000000001','10000000-0000-4000-8000-000000000001','50000000-0000-4000-8000-000000000001','Sales_Daily_24M','cedar-day-1',now() - interval '1 day','synthetic_scenario','demo-v1','{"net_sales":"812.40"}','synthetic-cedar-sales'),
 ('40000000-0000-4000-8000-000000000002','10000000-0000-4000-8000-000000000002','50000000-0000-4000-8000-000000000002','Sales_Daily_24M','birch-day-1',now() - interval '1 day','synthetic_scenario','demo-v1','{"net_sales":"517.25"}','synthetic-birch-sales');
