SELECT payload->>'name' AS name, ARRAY[1, 2] AS items FROM events;
SELECT DISTINCT ON (id) id, name FROM events ORDER BY id;
