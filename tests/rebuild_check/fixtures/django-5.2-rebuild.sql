CREATE TABLE "new__accounts" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "email" text NOT NULL UNIQUE, "balance" real NOT NULL);
INSERT INTO "new__accounts" ("id", "email", "balance") SELECT "id", "email", "balance" FROM "accounts";
DROP TABLE "accounts";
ALTER TABLE "new__accounts" RENAME TO "accounts";
CREATE INDEX "balance_idx" ON "accounts" ("balance") WHERE "balance" > 0.0;
