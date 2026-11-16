import sys

from datawrench.cli import main

raise SystemExit(main(["tapcheck", *sys.argv[1:]]))
