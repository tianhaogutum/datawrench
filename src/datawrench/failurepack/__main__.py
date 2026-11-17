import sys

from datawrench.cli import main

raise SystemExit(main(["failurepack", *sys.argv[1:]]))
