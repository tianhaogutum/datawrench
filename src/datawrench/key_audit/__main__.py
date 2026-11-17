import sys

from datawrench.cli import main

raise SystemExit(main(["key-audit", *sys.argv[1:]]))
