MODEL (
  name analytics.stg_orders,
  kind VIEW,
  columns (order_id INT, customer_id INT, amount DOUBLE, ds DATE),
  column_descriptions (order_id = 'Order identifier'),
  tags (staging)
);

SELECT order_id, customer_id, amount, ds FROM raw.raw_orders;
