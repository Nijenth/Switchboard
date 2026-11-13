-- ---------------------------------------------------------------------
-- Fixtures (as owner). Northwind = 10000000-…-0001 from the seed.
-- A second tenant "Acme" proves isolation.
-- ---------------------------------------------------------------------
INSERT INTO tenants (id, slug, name) VALUES ('91000000-0000-0000-0000-000000000001', 'acme-test', 'Acme Test Co');
INSERT INTO tenant_packs (id, tenant_id, pack_key, pack_id) VALUES
  ('91000000-0000-0000-0000-000000000002', '91000000-0000-0000-0000-000000000001', 'ecommerce_support', '30000000-0000-0000-0000-000000000001');
INSERT INTO staff_users (id, tenant_id, email, name, role) VALUES
  ('91000000-0000-0000-0000-000000000003', '91000000-0000-0000-0000-000000000001', 'admin@acme.example', 'Acme Admin', 'admin');
INSERT INTO channel_endpoints (id, tenant_id, tenant_pack_id, channel, address) VALUES
  ('91000000-0000-0000-0000-000000000005', '91000000-0000-0000-0000-000000000001', '91000000-0000-0000-0000-000000000002', 'email', 'support@acme.example');
INSERT INTO cases (id, tenant_id, tenant_pack_id, pack_id, channel, external_thread_id, endpoint_id) VALUES
  ('91000000-0000-0000-0000-000000000004', '91000000-0000-0000-0000-000000000001', '91000000-0000-0000-0000-000000000002',
   '30000000-0000-0000-0000-000000000001', 'email', 'acme-th-1', '91000000-0000-0000-0000-000000000005');

UPDATE tenant_packs SET enabled = false WHERE id = '91000000-0000-0000-0000-000000000002';

-- Northwind invoice case + message
INSERT INTO cases (id, tenant_id, tenant_pack_id, pack_id, channel, external_thread_id, endpoint_id) VALUES
  ('92000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000003',
   '30000000-0000-0000-0000-000000000003', 'email', 'nw-invoice-th-1',
   (SELECT id FROM channel_endpoints WHERE channel = 'email' AND address = 'invoices@northwind.example'));
INSERT INTO cases (tenant_id, channel, external_thread_id) VALUES
  ('10000000-0000-0000-0000-000000000001', 'email', 'nw-th-1');
INSERT INTO messages (id, tenant_id, case_id, endpoint_id, trace_id, idempotency_key, direction, channel, body_text)
SELECT '92000000-0000-0000-0000-000000000002', tenant_id, id, endpoint_id,
       gen_random_uuid(), 'email:test-1', 'inbound', 'email', 'Invoice attached'
  FROM cases WHERE id = '92000000-0000-0000-0000-000000000001';
