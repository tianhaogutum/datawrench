"""Create a small, deliberately unconstrained warehouse for the CLI example."""

import argparse
from pathlib import Path

import duckdb

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--dirty", action="store_true", help="insert a duplicated customer key")
args = p.parse_args()
path = Path(__file__).with_name("demo.duckdb")
with duckdb.connect(str(path), config={"threads": "2", "memory_limit": "128MB"}) as db:
    db.execute("DROP TABLE IF EXISTS orders; DROP TABLE IF EXISTS customers")
    db.execute(
        "CREATE TABLE orders(id INTEGER, customer_id INTEGER, amount DECIMAL(10,2))"
    )
    db.execute("INSERT INTO orders VALUES (1,10,25.00),(2,10,15.00),(3,99,40.00)")
    db.execute("CREATE TABLE customers(id INTEGER, name VARCHAR)")
    db.execute("INSERT INTO customers VALUES (10,'Alice')")
    if args.dirty:
        db.execute("INSERT INTO customers VALUES (10,'Duplicate Alice')")
print(f"Created {'dirty' if args.dirty else 'clean'} example at {path}")
