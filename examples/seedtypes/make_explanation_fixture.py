"""Synthetic full-file explanation fixture; writes only CSV to stdout."""
import csv
import sys

writer = csv.writer(sys.stdout, lineterminator='\n')
writer.writerow(['zip', 'amount', 'event_date'])
for line in range(2, 25_002):
    writer.writerow(['02134', '1.' + '1234567890' * 3 + '12345678', 'private-event']
                    if line == 21_000 else ['2134', '1.00', '2026-11-11'])
