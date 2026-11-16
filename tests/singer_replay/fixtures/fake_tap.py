"""Offline synthetic Singer tap; no credentials, network, or external data."""
import argparse
import sys
from pathlib import Path

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args()
    sys.stdout.write(Path(__file__).with_name("shop.jsonl").read_text())
    sys.stdout.flush()
    raise SystemExit(args.exit_code)
