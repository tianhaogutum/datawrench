import sys

from datawrench.cli import main

raise SystemExit(main(["contract-diff", *sys.argv[1:]]))
