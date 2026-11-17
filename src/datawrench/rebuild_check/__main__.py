import sys

from datawrench.cli import main

raise SystemExit(main(["rebuild-check", *sys.argv[1:]]))
