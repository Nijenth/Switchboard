-- =====================================================================
-- 001_demo_seed.sql — demo tenant "Northwind Apparel" running 3 packs
-- Place at: db/seeds/001_demo_seed.sql. Run once on an empty, migrated DB.
-- All companies, people and documents are fictional. Dates are relative to now().
--
-- Load the pack catalog before running this demo seed.
-- =====================================================================
BEGIN;

-- ---------------------------------------------------------------------
-- Tenant, settings, staff, queues
-- ---------------------------------------------------------------------
INSERT INTO tenants (id, slug, name) VALUES
  ('10000000-0000-0000-0000-000000000001', 'northwind', 'Northwind Apparel');

INSERT INTO tenant_settings (tenant_id, monthly_ai_budget_usd, brand_voice, approval_sla_minutes, timezone) VALUES
  ('10000000-0000-0000-0000-000000000001', 25.00,
   'Warm, concise and practical. Use the person''s first name. No emojis. Never promise dates we cannot confirm.',
   30, 'America/New_York');

INSERT INTO staff_users (id, tenant_id, email, name, role, slack_user_id) VALUES
  ('20000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 'priya@northwind.example',  'Priya (Support Agent)', 'agent',   'U-DEMO-PRIYA'),
  ('20000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 'marco@northwind.example',  'Marco (Ops Manager)',   'manager', 'U-DEMO-MARCO'),
  ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 'fatima@northwind.example', 'Fatima (Finance)',      'finance', 'U-DEMO-FATIMA'),
  ('20000000-0000-0000-0000-000000000004', '10000000-0000-0000-0000-000000000001', 'alex@northwind.example',   'Alex (Admin)',          'admin',   'U-DEMO-ALEX'),
  ('20000000-0000-0000-0000-000000000005', '10000000-0000-0000-0000-000000000001', 'sam@northwind.example',    'Sam (Sales Rep)',       'agent',   'U-DEMO-SAM');

INSERT INTO queues (tenant_id, key, name, notify_channel) VALUES
  ('10000000-0000-0000-0000-000000000001', 'customer_support', 'Customer Support',  'C-DEMO-SUPPORT'),
  ('10000000-0000-0000-0000-000000000001', 'sales',            'Sales',             'C-DEMO-SALES'),
  ('10000000-0000-0000-0000-000000000001', 'accounts_payable', 'Accounts Payable',  'C-DEMO-AP'),
  ('10000000-0000-0000-0000-000000000001', 'escalations',      'Escalations',       'C-DEMO-ESCALATIONS');

-- ---------------------------------------------------------------------
-- Tenant packs + inbound endpoints
-- ---------------------------------------------------------------------
INSERT INTO tenant_packs (id, tenant_id, pack_key, pack_id, config) VALUES
  ('40000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 'ecommerce_support',
   (SELECT id FROM public.packs WHERE key='ecommerce_support' AND version=2),
   '{"adapters":{"order_lookup":"mock","order_refund_eligibility":"mock","order_refund_issue":"mock","send_reply":"mock"}}'),
  ('40000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 'sales_leads',        '30000000-0000-0000-0000-000000000002',
   '{"adapters":{"company_enrich":"mock","crm_upsert_contact":"mock","crm_create_deal":"mock","calendar_find_slots":"mock","meeting_book":"mock"},
     "sales_rep_email":"sam@northwind.example"}'),
  ('40000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 'invoice_processing', '30000000-0000-0000-0000-000000000003',
   '{"adapters":{"vendor_lookup":"mock","po_lookup":"mock","invoice_duplicate_check":"mock","invoice_post_to_accounting":"mock","invoice_schedule_payment":"mock"}}');

INSERT INTO channel_endpoints (tenant_id, tenant_pack_id, channel, address) VALUES
  ('10000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001', 'email',    'support@northwind.example'),
  ('10000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001', 'web_chat', 'northwind-site-chat'),
  ('10000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000002', 'web_form', 'northwind-wholesale-form'),
  ('10000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000002', 'email',    'sales@northwind.example'),
  ('10000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000003', 'email',    'invoices@northwind.example'),
  ('10000000-0000-0000-0000-000000000001', NULL,                                   'email',    'hello@northwind.example');  -- shared inbox → AI router

-- ---------------------------------------------------------------------
-- Knowledge base (embeddings filled by wf_kb_ingest)
-- ---------------------------------------------------------------------
INSERT INTO knowledge_documents (id, tenant_id, pack_key, title, doc_type, checksum) VALUES
  ('50000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 'ecommerce_support',  'Payments FAQ',               'faq',       'seed-3'),
  ('50000000-0000-0000-0000-000000000004', '10000000-0000-0000-0000-000000000001', 'sales_leads',        'Wholesale Program',          'pricing',   'seed-4'),
  ('50000000-0000-0000-0000-000000000005', '10000000-0000-0000-0000-000000000001', 'sales_leads',        'Lead Scoring Guide',         'procedure', 'seed-5'),
  ('50000000-0000-0000-0000-000000000006', '10000000-0000-0000-0000-000000000001', 'invoice_processing', 'Accounts Payable Procedure', 'procedure', 'seed-6'),
  ('50000000-0000-0000-0000-000000000007', '10000000-0000-0000-0000-000000000001', NULL,                 'Company Overview',           'other',     'seed-7');

INSERT INTO knowledge_chunks (tenant_id, document_id, pack_key, chunk_index, content) VALUES
  ('10000000-0000-0000-0000-000000000001', '50000000-0000-0000-0000-000000000003', 'ecommerce_support', 0,
   'We accept Visa, Mastercard, American Express, PayPal and Apple Pay. If a payment fails the order is held for 48 hours so the customer can retry.'),
  ('10000000-0000-0000-0000-000000000001', '50000000-0000-0000-0000-000000000004', 'sales_leads', 0,
   'Wholesale pricing starts at 15 units per style. Tiers: 15-49 units 35% off retail, 50-199 units 45% off, 200+ units custom quote. Minimum first order is $1,500.'),
  ('10000000-0000-0000-0000-000000000001', '50000000-0000-0000-0000-000000000004', 'sales_leads', 1,
   'Wholesale partners must be registered retailers. Lead time is 3-4 weeks for in-stock styles. Sales replies to wholesale enquiries within one business day.'),
  ('10000000-0000-0000-0000-000000000001', '50000000-0000-0000-0000-000000000005', 'sales_leads', 0,
   'Lead score (0-100): +30 registered retailer, +20 two or more stores, +20 order intent 50+ units, +15 target country (US, UK, EU), +15 business email domain. Leads scoring 60+ are qualified and offered a meeting.'),
  ('10000000-0000-0000-0000-000000000001', '50000000-0000-0000-0000-000000000006', 'invoice_processing', 0,
   'Every supplier invoice must reference an open purchase order. A total within 2% of the PO total is a clean match and can be posted to accounting automatically.'),
  ('10000000-0000-0000-0000-000000000001', '50000000-0000-0000-0000-000000000006', 'invoice_processing', 1,
   'Invoices of $5,000 or more need manager approval. All payments need finance approval. Standard payment terms are net 30.'),
  ('10000000-0000-0000-0000-000000000001', '50000000-0000-0000-0000-000000000006', 'invoice_processing', 2,
   'Any request to change supplier bank details is treated as potential fraud: escalate immediately and verify by phone using the number on file, never the number in the email.'),
  ('10000000-0000-0000-0000-000000000001', '50000000-0000-0000-0000-000000000007', NULL, 0,
   'Northwind Apparel is a US clothing brand selling outerwear and basics online and through wholesale partners. Support hours are 9am-6pm Eastern, Monday to Friday.');

-- ---------------------------------------------------------------------
-- DEMO: mock shop orders (each one is a scenario)
-- ---------------------------------------------------------------------
INSERT INTO demo.shop_orders (id, tenant_id, order_number, customer_email, customer_name, shipping_postcode, shipping_country,
                              status, total_amount, currency, carrier, tracking_number, placed_at, shipped_at, delivered_at,
                              estimated_delivery_at, refunded_at, scenario) VALUES
  ('60000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 'NW-1001', 'emma.jones@example.com',   'Emma Jones',   '94107',   'US',
   'shipped',        129.00, 'USD', 'UPS',   '1Z999AA10000000001',     now()-interval '5 days',  now()-interval '4 days',  NULL,                     now()+interval '2 days', NULL, 'in_transit_on_time'),
  ('60000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 'NW-1002', 'liam.chen@example.com',    'Liam Chen',    '10001',   'US',
   'shipped',         89.50, 'USD', 'USPS',  '9400100000000000000002', now()-interval '14 days', now()-interval '12 days', NULL,                     now()-interval '4 days', NULL, 'late_delivery'),
  ('60000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 'NW-1003', 'sofia.rossi@example.com',  'Sofia Rossi',  '20121',   'IT',
   'delivered',      210.00, 'EUR', 'DHL',   'JD014600000000000003',   now()-interval '18 days', now()-interval '16 days', now()-interval '10 days', NULL,                    NULL, 'refund_eligible'),
  ('60000000-0000-0000-0000-000000000004', '10000000-0000-0000-0000-000000000001', 'NW-1004', 'arjun.mehta@example.com',  'Arjun Mehta',  '560001',  'IN',
   'delivered',       64.00, 'USD', 'FedEx', '771200000000004',        now()-interval '70 days', now()-interval '68 days', now()-interval '60 days', NULL,                    NULL, 'refund_outside_window'),
  ('60000000-0000-0000-0000-000000000005', '10000000-0000-0000-0000-000000000001', 'NW-1005', 'olivia.brown@example.com', 'Olivia Brown', 'SW1A1AA', 'GB',
   'payment_failed', 155.00, 'GBP', NULL,    NULL,                     now()-interval '1 day',   NULL,                     NULL,                     NULL,                    NULL, 'payment_failed'),
  ('60000000-0000-0000-0000-000000000006', '10000000-0000-0000-0000-000000000001', 'NW-1006', 'noah.wilson@example.com',  'Noah Wilson',  '60601',   'US',
   'processing',      42.00, 'USD', NULL,    NULL,                     now()-interval '1 day',   NULL,                     NULL,                     now()+interval '5 days', NULL, 'not_shipped_yet'),
  ('60000000-0000-0000-0000-000000000007', '10000000-0000-0000-0000-000000000001', 'NW-1007', 'emma.jones@example.com',   'Emma Jones',   '94107',   'US',
   'refunded',        75.00, 'USD', 'UPS',   '1Z999AA10000000007',     now()-interval '40 days', now()-interval '38 days', now()-interval '33 days', NULL,                    now()-interval '3 days', 'refund_issued'),
  ('60000000-0000-0000-0000-000000000008', '10000000-0000-0000-0000-000000000001', 'NW-1008', 'sofia.rossi@example.com', 'Sofia Rossi', '20121', 'IT',
   'delivered',      32.00, 'EUR', 'DHL', 'JD014600000000000008', now()-interval '9 days', now()-interval '8 days', now()-interval '4 days', NULL, NULL, 'refund_review_fixture');

INSERT INTO demo.shop_order_items (order_id, sku, name, quantity, unit_price) VALUES
  ('60000000-0000-0000-0000-000000000001', 'JKT-DEN-M',   'Denim Jacket (M)',       1, 129.00),
  ('60000000-0000-0000-0000-000000000002', 'TEE-ORG-L',   'Organic Cotton Tee (L)', 2,  29.75),
  ('60000000-0000-0000-0000-000000000002', 'CAP-WOOL',    'Wool Cap',               1,  30.00),
  ('60000000-0000-0000-0000-000000000003', 'COAT-WL-S',   'Wool Overcoat (S)',      1, 210.00),
  ('60000000-0000-0000-0000-000000000004', 'SHRT-LIN-M',  'Linen Shirt (M)',        1,  64.00),
  ('60000000-0000-0000-0000-000000000005', 'BOOT-LTH-42', 'Leather Boots (EU 42)',  1, 155.00),
  ('60000000-0000-0000-0000-000000000006', 'SCF-CSH',     'Cashmere Scarf',         1,  42.00),
  ('60000000-0000-0000-0000-000000000007', 'HOOD-GRY-M',  'Grey Hoodie (M)',        1,  75.00);

-- ---------------------------------------------------------------------
-- DEMO: mock CRM, enrichment directory, calendar
-- ---------------------------------------------------------------------
INSERT INTO demo.crm_contacts (tenant_id, email, full_name, company, lifecycle, lead_score) VALUES
  ('10000000-0000-0000-0000-000000000001', 'owner@harborlanegoods.example', 'Dana Harbor', 'Harbor Lane Goods', 'customer', 88);

INSERT INTO demo.company_directory (domain, name, industry, employee_count, country, store_count) VALUES
  ('maplethreads.example',     'Maple Threads Boutiques', 'Apparel retail',      85, 'US', 6),
  ('cornerclosetshop.example', 'Corner Closet',           'Apparel retail',       4, 'US', 1),
  ('nordmode.example',         'Nordmode AB',             'Department stores',  1200, 'SE', 22),
  ('harborlanegoods.example',  'Harbor Lane Goods',       'Apparel retail',      30, 'US', 3),
  ('growthleadsagency.example','Growth Leads Agency',     'Marketing services',  15, 'US', 0);

INSERT INTO demo.calendar_slots (tenant_id, owner_email, starts_at, ends_at) VALUES
  ('10000000-0000-0000-0000-000000000001', 'sam@northwind.example', date_trunc('day', now()) + interval '1 day 15 hours',  date_trunc('day', now()) + interval '1 day 15 hours 30 minutes'),
  ('10000000-0000-0000-0000-000000000001', 'sam@northwind.example', date_trunc('day', now()) + interval '1 day 19 hours',  date_trunc('day', now()) + interval '1 day 19 hours 30 minutes'),
  ('10000000-0000-0000-0000-000000000001', 'sam@northwind.example', date_trunc('day', now()) + interval '2 days 14 hours', date_trunc('day', now()) + interval '2 days 14 hours 30 minutes'),
  ('10000000-0000-0000-0000-000000000001', 'sam@northwind.example', date_trunc('day', now()) + interval '3 days 16 hours', date_trunc('day', now()) + interval '3 days 16 hours 30 minutes');

-- ---------------------------------------------------------------------
-- DEMO: mock accounting — vendors, purchase orders, existing bills
-- ---------------------------------------------------------------------
INSERT INTO demo.vendors (id, tenant_id, name, email_domain, tax_id, payment_terms_days, bank_last4) VALUES
  ('70000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 'Loom & Thread Textiles', 'loomandthread.example', 'US-41-5550101', 30, '4821'),
  ('70000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 'Button & Zip Supply',    'buttonzip.example',     'US-41-5550102', 15, '7730'),
  ('70000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 'Pacific Freight Co',     'pacificfreight.example','US-41-5550103', 30, '1194');

INSERT INTO demo.purchase_orders (id, tenant_id, vendor_id, po_number, status, total_amount, currency, issued_at) VALUES
  ('71000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', '70000000-0000-0000-0000-000000000001', 'PO-2001', 'open', 4800.00, 'USD', now()-interval '20 days'),
  ('71000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', '70000000-0000-0000-0000-000000000002', 'PO-2002', 'open', 1250.00, 'USD', now()-interval '12 days'),
  ('71000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', '70000000-0000-0000-0000-000000000001', 'PO-2003', 'open', 9600.00, 'USD', now()-interval '8 days');

INSERT INTO demo.purchase_order_lines (po_id, sku, description, quantity, unit_price) VALUES
  ('71000000-0000-0000-0000-000000000001', 'FAB-DEN-12', '12oz selvedge denim (yards)',   400, 12.00),
  ('71000000-0000-0000-0000-000000000002', 'BTN-CPR-20', 'Copper shank buttons 20mm',    5000,  0.15),
  ('71000000-0000-0000-0000-000000000002', 'ZIP-MTL-60', 'Metal zippers 60cm',            500,  1.00),
  ('71000000-0000-0000-0000-000000000003', 'FAB-WOL-MER', 'Merino wool fabric (yards)',   600, 16.00);

INSERT INTO demo.bills (tenant_id, vendor_id, invoice_number, po_id, total_amount, currency, status) VALUES
  ('10000000-0000-0000-0000-000000000001', '70000000-0000-0000-0000-000000000002', 'BZ-7781', '71000000-0000-0000-0000-000000000002', 1250.00, 'USD', 'awaiting_payment');

INSERT INTO audit_log (tenant_id, actor_type, actor_id, event_type, entity_type, entity_id, details) VALUES
  ('10000000-0000-0000-0000-000000000001', 'system', 'seed', 'tenant.seeded', 'tenant',
   '10000000-0000-0000-0000-000000000001', '{"seed":"001_demo_seed","packs":["ecommerce_support","sales_leads","invoice_processing"]}');

COMMIT;
