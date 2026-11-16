-- duckmate schema-only snapshot v1 (sequence cursors and user rows omitted)
CREATE TABLE _duckmate_checksums(migrations_table VARCHAR, "version" VARCHAR, checksum VARCHAR NOT NULL, PRIMARY KEY(migrations_table, "version"));
CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id BIGINT NOT NULL, amount DECIMAL(12,2) NOT NULL);
CREATE TABLE schema_migrations("version" VARCHAR PRIMARY KEY);
INSERT INTO "schema_migrations" (version) VALUES ('20261101090000');
INSERT INTO "_duckmate_checksums" VALUES ('schema_migrations', '20261101090000', 'cd9a0e01c292adfc8e7165801b0617280b979fdb746ade78f8f3c76cc47372b5');
