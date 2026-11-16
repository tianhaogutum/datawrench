import sys

from datawrench.cli import main

raise SystemExit(main(["duckmate", *sys.argv[1:]]))
