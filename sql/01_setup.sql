-- Pattern B: one shared SP + OAuth custom identity claim.
-- Substitute ${catalog}/${schema} (defaults: nt_workspace_catalog / kustom_claims_demo).
--
-- IMPORTANT execution order / identity (see README "Setup gotcha"):
--   * current_oauth_custom_identity_claim() is EAGER-EVALUATED during CREATE FUNCTION
--     and ALTER TABLE ... SET ROW FILTER. Those two statements must run in a session
--     that CARRIES a claim — i.e. as the shared SP using a claim-bearing token — and the
--     SP must own the table. provision.py does this automatically.
--   * The schema / table / data / grants can be created by a normal admin.

CREATE SCHEMA IF NOT EXISTS ${catalog}.${schema};

CREATE OR REPLACE TABLE ${catalog}.${schema}.orders (
  order_id    STRING,
  tenant_id   STRING,
  order_date  DATE,
  product     STRING,
  category    STRING,
  amount      DECIMAL(12,2),
  customer_id STRING
);

INSERT INTO ${catalog}.${schema}.orders VALUES
  ('O-1001','M001',DATE'2026-05-04','Espresso Beans 1kg','Beverages',14.75,'C-1'),
  ('O-1002','M001',DATE'2026-05-11','Oat Milk Case','Beverages',42.00,'C-2'),
  ('O-1003','M001',DATE'2026-05-19','Pastry Box','Food',28.50,'C-1'),
  ('O-2001','M002',DATE'2026-05-03','Running Shoes','Footwear',120.00,'C-7'),
  ('O-2002','M002',DATE'2026-05-09','Compression Tee','Apparel',45.00,'C-8'),
  ('O-2003','M002',DATE'2026-05-22','Trail Jacket','Apparel',180.00,'C-9'),
  ('O-3001','M003',DATE'2026-05-02','Cordless Drill','Tools',249.00,'C-4'),
  ('O-3002','M003',DATE'2026-05-14','Steel Bolts 500ct','Hardware',88.00,'C-5'),
  ('O-3003','M003',DATE'2026-05-27','Workbench','Furniture',420.00,'C-6');

-- Hand the table to the shared SP so it can create/attach the claim-based filter.
ALTER TABLE ${catalog}.${schema}.orders OWNER TO `<shared-sp-app-id>`;

-- ---- The following two run AS THE SHARED SP with a claim-bearing token ----

-- Row filter: a row is visible only if its tenant_id equals the claim in the caller's token.
CREATE OR REPLACE FUNCTION ${catalog}.${schema}.rf_tenant(tid STRING)
  RETURN tid = current_oauth_custom_identity_claim();

ALTER TABLE ${catalog}.${schema}.orders
  SET ROW FILTER ${catalog}.${schema}.rf_tenant ON (tenant_id);
