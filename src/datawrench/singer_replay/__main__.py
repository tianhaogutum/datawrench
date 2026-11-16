import sys

from datawrench.cli import main

raise SystemExit(main(["singer-replay", *sys.argv[1:]]))
