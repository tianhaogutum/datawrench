-- Generated gc_007_preservation; zero rows means pass.
-- depends_on: {{ ref('orders') }}
SELECT
  k0,
  SUM(before_rows) AS expected_rows,
  SUM(after_rows) AS actual_rows
FROM (
  SELECT
    k0,
    COUNT(*) AS before_rows,
    0 AS after_rows
  FROM (
    SELECT
      o.id AS k0
    FROM orders AS o
  ) AS gc_before
  GROUP BY
    k0
  UNION ALL
  SELECT
    k0,
    0 AS before_rows,
    COUNT(*) AS after_rows
  FROM (
    SELECT
      o.id AS k0
    FROM orders AS o
    LEFT JOIN customers AS c
      ON o.customer_id = c.id
  ) AS gc_after
  GROUP BY
    k0
) AS gc_bag
GROUP BY
  k0
HAVING
  SUM(before_rows) <> SUM(after_rows)
