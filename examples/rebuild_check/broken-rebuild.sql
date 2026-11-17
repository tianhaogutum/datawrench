-- Typical handwritten rebuild: rows are retained, dependencies are forgotten.
PRAGMA foreign_keys=OFF;
BEGIN;
CREATE TABLE accounts_new(id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, balance REAL DEFAULT 0);
INSERT INTO accounts_new SELECT * FROM accounts;
DROP TABLE accounts;
PRAGMA legacy_alter_table=ON;
ALTER TABLE accounts_new RENAME TO accounts;
COMMIT;
PRAGMA foreign_keys=ON;
