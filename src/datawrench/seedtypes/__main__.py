import sys

from datawrench.cli import main

raise SystemExit(main(["seedtypes", *sys.argv[1:]]))
