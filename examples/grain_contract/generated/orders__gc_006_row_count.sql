-- Generated gc_006_row_count; zero rows means pass.
-- depends_on: {{ ref('orders') }}
SELECT
  *
FROM (
  SELECT
    (
      SELECT
        COUNT(*)
      FROM (
        SELECT
          1
        FROM orders AS o
      ) AS gc_before
    ) AS expected_rows,
    (
      SELECT
        COUNT(*)
      FROM (
        SELECT
          1
        FROM orders AS o
        LEFT JOIN customers AS c
          ON o.customer_id = c.id
      ) AS gc_after
    ) AS actual_rows
) AS gc_counts
WHERE
  expected_rows <> actual_rows
