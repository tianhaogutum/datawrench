-- A synthetic migration report: three ready statements, two with problems.
SELECT id, amount FROM orders;

SELECT customer_id, SUM(amount) AS total FROM orders GROUP BY customer_id;

SELECT DATE_TRUNC('month', created_at) AS month FROM orders;

SELECT MY_UDF(amount) AS score FROM orders;

SELECT missing_column FROM orders;
