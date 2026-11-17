-- Generated gc_005_join_cardinality; zero rows means pass.
-- depends_on: {{ ref('orders') }}
SELECT
  k0,
  COUNT(*) AS occurrences
FROM (
  SELECT
    o.id AS k0
  FROM orders AS o
  INNER JOIN customers AS c
    ON o.customer_id = c.id
) AS gc_keys
WHERE
  NOT k0 IS NULL
GROUP BY
  k0
HAVING
  COUNT(*) > 1
