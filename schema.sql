-- Supplier Qualification Database
-- Product: Luxury Towels
-- Geography: China, Taiwan, Japan, Southeast Asia

DROP TABLE IF EXISTS suppliers;

CREATE TABLE suppliers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_name TEXT,
  legal_name TEXT,
  factory_locations TEXT,
  supplier_type TEXT,
  supplier_subtype TEXT,
  flags TEXT,
  product_categories TEXT,
  product_sub_categories TEXT,
  certs_and_audits TEXT,
  regulatory_compliance TEXT,
  brands_worked_with TEXT,
  contact_name TEXT,
  date_created TEXT,
  market_experience TEXT,
  certification_link TEXT,
  ip_ownership TEXT,
  nda_link TEXT,
  nda_start TEXT,
  non_circumvent_period_months INTEGER,
  agreement_link TEXT,
  agreement_notes TEXT,
  term_notes TEXT,
  date_outreached TEXT,
  date_qualified TEXT,
  moq TEXT,
  moq_info TEXT
);
