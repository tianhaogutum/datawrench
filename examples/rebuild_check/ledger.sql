-- Example ledger: an audit trigger, a partial index, a UNIQUE column, a view and an inbound
-- cascading foreign key. Used by create_ledger.py and by tests/rebuild_check.
PRAGMA foreign_keys=ON;
CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, balance INTEGER DEFAULT 0);
CREATE TABLE entries (id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE, amount INTEGER);
CREATE TABLE audit (account_id INTEGER, event TEXT);
CREATE INDEX entries_account_idx ON entries(account_id);
CREATE INDEX balance_idx ON accounts(balance) WHERE balance > 0;
CREATE TRIGGER account_audit AFTER UPDATE OF balance ON accounts BEGIN INSERT INTO audit VALUES(new.id, 'balance changed'); END;
CREATE VIEW balances AS SELECT id, balance FROM accounts;
INSERT INTO accounts VALUES(1, 'a@example.test', 200), (2, 'b@example.test', 0);
INSERT INTO entries VALUES(1, 1, 200);
