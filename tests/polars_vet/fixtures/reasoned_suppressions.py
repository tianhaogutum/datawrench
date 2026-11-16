import polars as p
from polars import col as c, read_csv as read
q = read("data.csv").lazy(); expr = c("x").map_elements(lambda x: x + 1)  # polars-vet: ignore[PLV001] -- preserve eager validation
another = p.read_csv("data.csv").lazy()
reviewed = read("data.csv").lazy(); native_candidate = c("x").map_elements(lambda x: x + 1)  # polars-vet: ignore[PLV001, PLV002] -- reviewed eager boundary and callback
print(q.collect().to_dict(as_series=False))
