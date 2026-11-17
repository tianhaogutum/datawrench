-- Generated gc_004_join_key; zero rows means pass.
-- depends_on: {{ ref('orders') }}
SELECT
  k0,
  COUNT(*) AS occurrences
FROM (
  SELECT
    c.id AS k0
  FROM customers AS c
) AS gc_keys
WHERE
  NOT k0 IS NULL
GROUP BY
  k0
HAVING
  COUNT(*) > 1
