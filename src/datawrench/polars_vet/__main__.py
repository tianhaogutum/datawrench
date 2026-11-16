import sys

from datawrench.cli import main

raise SystemExit(main(["polars-vet", *sys.argv[1:]]))
