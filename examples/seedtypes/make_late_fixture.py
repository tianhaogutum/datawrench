"""Synthetic redistributable sample; writes only CSV to stdout."""
import csv
import sys

writer = csv.writer(sys.stdout, lineterminator='\n')
writer.writerow(['zip'])
for line in range(2, 25_002):
    writer.writerow(['02134' if line == 21_000 else '2134'])
