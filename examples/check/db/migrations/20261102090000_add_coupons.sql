-- migrate:up
ALTER TABLE orders ADD COLUMN coupon_code VARCHAR;
CREATE TEMP TABLE coupon_backfill AS SELECT id FROM orders;

-- migrate:down
