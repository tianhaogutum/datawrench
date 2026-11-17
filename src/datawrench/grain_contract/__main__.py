import sys

from datawrench.cli import main

raise SystemExit(main(["grain-contract", *sys.argv[1:]]))
