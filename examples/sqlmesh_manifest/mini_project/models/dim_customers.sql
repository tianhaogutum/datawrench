MODEL (
  name analytics.dim_customers,
  kind FULL,
  columns (customer_id INT, name TEXT)
);

SELECT customer_id, name FROM raw.customers;
