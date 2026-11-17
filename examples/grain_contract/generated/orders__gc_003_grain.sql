-- Generated gc_003_grain; zero rows means pass.
-- depends_on: {{ ref('orders') }}
SELECT
  k0,
  COUNT(*) AS occurrences
FROM (
  SELECT
    o.id AS k0
  FROM orders AS o
) AS gc_keys
GROUP BY
  k0
HAVING
  COUNT(*) > 1 OR k0 IS NULL
