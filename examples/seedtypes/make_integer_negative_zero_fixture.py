"""Synthetic BIGINT negative-zero boundary; CSV to stdout with no null policy."""
import csv
import sys

writer = csv.writer(sys.stdout, lineterminator='\n')
writer.writerow(['integer', 'note'])
for row in range(25_000):
    # A preceding multiline record puts the negative zero on physical line 21,000.
    writer.writerow(['-0' if row == 20_997 else '12',
                     'multi\nline' if row == 20_996 else 'plain'])
