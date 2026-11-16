-- migrate:up
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id BIGINT NOT NULL,
    amount DECIMAL(12, 2) NOT NULL
);

-- migrate:down
DROP TABLE orders;
