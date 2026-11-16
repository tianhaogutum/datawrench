import sys

from datawrench.cli import main

raise SystemExit(main(["lint-cache", *sys.argv[1:]]))
