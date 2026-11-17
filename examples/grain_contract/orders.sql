SELECT
  o.id AS order_id,
  c.name AS customer_name,
  o.amount
FROM orders AS o
LEFT JOIN customers AS c
  ON o.customer_id = c.id
