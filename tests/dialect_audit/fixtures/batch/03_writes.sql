-- These statements must not create a table or load rows.
CREATE TABLE audit_changed (id INT);
INSERT INTO audit_changed VALUES (1);
SELECT * FROM audit_changed;
SELECT 3;
