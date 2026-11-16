import sys

from datawrench.cli import main

raise SystemExit(main(["parquet-unify", *sys.argv[1:]]))
