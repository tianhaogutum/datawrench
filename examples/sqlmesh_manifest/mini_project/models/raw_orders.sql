MODEL (
  name raw.raw_orders,
  kind SEED (path '../seeds/raw_orders.csv'),
  columns (order_id INT, customer_id INT, amount DOUBLE, ds DATE),
  column_descriptions (order_id = 'Synthetic order identifier')
);
