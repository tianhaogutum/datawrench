import sys

from datawrench.cli import main

raise SystemExit(main(["dialect-audit", *sys.argv[1:]]))
