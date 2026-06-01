-- Grants for the single shared SP. Just ONE set of grants, regardless of tenant count.

GRANT USE CATALOG    ON CATALOG  ${catalog} TO `<shared-sp-app-id>`;
GRANT USE SCHEMA     ON SCHEMA   ${catalog}.${schema} TO `<shared-sp-app-id>`;
GRANT CREATE FUNCTION ON SCHEMA  ${catalog}.${schema} TO `<shared-sp-app-id>`;  -- to create rf_tenant
GRANT SELECT         ON TABLE    ${catalog}.${schema}.orders TO `<shared-sp-app-id>`;
GRANT EXECUTE        ON FUNCTION ${catalog}.${schema}.rf_tenant TO `<shared-sp-app-id>`;

-- Not expressible in SQL — do these via the REST API / CLI (once, for the shared SP):
--
-- Warehouse CAN_USE:
--   PATCH /api/2.0/permissions/warehouses/{warehouse_id}
--   {"access_control_list":[{"service_principal_name":"<shared-sp-app-id>","permission_level":"CAN_USE"}]}
--
-- Genie space CAN_RUN (space over <catalog>.<schema>.orders, run_as = VIEWER):
--   PATCH /api/2.0/permissions/genie/{space_id}
--   {"access_control_list":[{"service_principal_name":"<shared-sp-app-id>","permission_level":"CAN_RUN"}]}
--
-- That's it — no per-tenant grants. New tenants need zero Databricks-side changes.
