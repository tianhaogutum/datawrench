import sys

from datawrench.cli import main

raise SystemExit(main(["csv-rewind", *sys.argv[1:]]))
