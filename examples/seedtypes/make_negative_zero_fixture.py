"""Synthetic negative-zero boundary; CSV to stdout only, no null policy needed."""
import csv
import sys

writer = csv.writer(sys.stdout, lineterminator='\n')
writer.writerow(['amount', 'note'])
for row in range(25_000):
    # A preceding multiline record puts the negative zero on physical line 21,000.
    writer.writerow(['-0.00' if row == 20_997 else '1.00',
                     'multi\nline' if row == 20_996 else 'plain'])
