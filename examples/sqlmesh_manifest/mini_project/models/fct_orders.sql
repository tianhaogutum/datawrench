MODEL (
  name analytics.fct_orders,
  kind INCREMENTAL_BY_TIME_RANGE (time_column ds),
  owner 'analytics-team',
  cron '@daily',
  grain order_id,
  tags (finance, gold),
  columns (order_id INT, customer_id INT, amount DOUBLE, ds DATE),
  audits (not_null(columns := (order_id)), unique_values(columns := (order_id)))
);

SELECT order_id, customer_id, amount, ds FROM analytics.stg_orders;
