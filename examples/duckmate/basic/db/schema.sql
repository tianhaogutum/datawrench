-- duckmate schema-only snapshot v1 (sequence cursors and user rows omitted)
CREATE TABLE _duckmate_checksums(migrations_table VARCHAR, "version" VARCHAR, checksum VARCHAR NOT NULL, PRIMARY KEY(migrations_table, "version"));
CREATE TABLE schema_migrations("version" VARCHAR PRIMARY KEY);
CREATE TABLE users(id INTEGER PRIMARY KEY, email VARCHAR NOT NULL UNIQUE);
INSERT INTO "schema_migrations" (version) VALUES ('20260101120000');
INSERT INTO "_duckmate_checksums" VALUES ('schema_migrations', '20260101120000', '9f67c5ea01b5fec68601370fb270b12d835efee129b04a3ba9d24997160bcea9');
