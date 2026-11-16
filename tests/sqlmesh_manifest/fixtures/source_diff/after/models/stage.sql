MODEL (name analytics.stage, kind VIEW, columns (id INT));

SELECT CAST(id AS INT) AS id FROM raw.input;
