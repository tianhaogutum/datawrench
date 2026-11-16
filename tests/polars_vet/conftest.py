"""Keep real Polars tests bounded on shared Linux hosts."""
import os

os.environ.setdefault('POLARS_MAX_THREADS', '2')
