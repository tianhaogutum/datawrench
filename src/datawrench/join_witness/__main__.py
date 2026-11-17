import sys

from datawrench.cli import main

raise SystemExit(main(["join-witness", *sys.argv[1:]]))
