import sys

from datawrench.cli import main

raise SystemExit(main(["sqlmesh-manifest", *sys.argv[1:]]))
