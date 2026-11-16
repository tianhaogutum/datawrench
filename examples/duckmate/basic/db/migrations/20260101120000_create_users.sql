-- migrate:up
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email VARCHAR NOT NULL UNIQUE
);
INSERT INTO users VALUES (1, 'sample@example.invalid');

-- migrate:down
DROP TABLE users;
