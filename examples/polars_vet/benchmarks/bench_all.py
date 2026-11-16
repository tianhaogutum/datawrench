"""Run all real Polars synthetic comparisons in one bounded process."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import run

if __name__ == '__main__':
    run()
