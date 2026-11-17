-- Generated gc_009_output_grain; zero rows means pass.
-- depends_on: {{ ref('orders') }}
SELECT
  k0,
  COUNT(*) AS occurrences
FROM (
  SELECT
    order_id AS k0
  FROM (
    SELECT
      o.id AS order_id,
      c.name AS customer_name,
      o.amount
    FROM orders AS o
    LEFT JOIN customers AS c
      ON o.customer_id = c.id
  ) AS gc_model
) AS gc_keys
GROUP BY
  k0
HAVING
  COUNT(*) > 1 OR k0 IS NULL
