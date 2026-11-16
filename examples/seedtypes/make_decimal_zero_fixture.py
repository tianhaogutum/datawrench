"""Synthetic scale-38 boundary; infer with --null-token NULL. CSV to stdout only."""
import csv
import sys

writer = csv.writer(sys.stdout, lineterminator='\n')
writer.writerow(['fraction', 'note'])
for row in range(25_000):
    # The multiline record puts the first non-null fraction on physical line 21,000.
    writer.writerow(['NULL' if row < 20_997 else '0.' + '1' * 38,
                     'multi\nline' if row == 20_996 else 'plain'])
