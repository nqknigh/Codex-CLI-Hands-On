"""Create an audited copy; never overwrite the source or impute unknown values."""
from pathlib import Path
import hashlib
import json

import pandas as pd


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "AmesHousing-1.csv"
OUTPUT = ROOT / "AmesHousing-1-cleaned.csv"
source_hash = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
raw = pd.read_csv(SOURCE, keep_default_na=False)
df = raw.copy()
changes = []
flags = []


def change(index, column, value, reason):
    old = df.at[index, column]
    df.at[index, column] = value
    changes.append({"Order": int(raw.at[index, "Order"]), "column": column,
                    "before": str(old), "after": None if pd.isna(value) else str(value),
                    "reason": reason})


def flag(mask, issue, columns):
    for index in raw.index[mask]:
        flags.append({"Order": int(raw.at[index, "Order"]),
                      "PID": str(raw.at[index, "PID"]), "issue": issue,
                      "source_values": json.dumps({c: str(raw.at[index, c]) for c in columns})})


# Strip text and represent actual blanks as missing without interpreting NA/None.
for column in df:
    if pd.api.types.is_string_dtype(df[column]):
        for index, value in df[column].items():
            stripped = value.strip()
            if stripped != value:
                change(index, column, stripped, "trim whitespace")
            if stripped == "":
                change(index, column, pd.NA, "source blank remains unknown or not applicable")


def numeric(column):
    return pd.to_numeric(df[column], errors="coerce")


# Explicit absence codes. For features with numeric evidence, require agreement.
absence = {
    "Alley": ("No alley", None),
    "Fence": ("No fence", None),
    "Misc Feature": ("No miscellaneous feature", numeric("Misc Val").eq(0)),
    "Fireplace Qu": ("No fireplace", numeric("Fireplaces").eq(0)),
    "Pool QC": ("No pool", numeric("Pool Area").eq(0)),
}
for column in ["Bsmt Qual", "Bsmt Cond", "Bsmt Exposure", "BsmtFin Type 1", "BsmtFin Type 2"]:
    absence[column] = ("No basement", numeric("Total Bsmt SF").eq(0))
no_garage = (df["Garage Type"].eq("NA") & numeric("Garage Cars").eq(0)
             & numeric("Garage Area").eq(0))
for column in ["Garage Type", "Garage Finish", "Garage Qual", "Garage Cond"]:
    absence[column] = ("No garage", no_garage)
for column, (label, evidence) in absence.items():
    coded = df[column].eq("NA").fillna(False)
    supported = coded if evidence is None else coded & evidence
    for index in df.index[supported]:
        change(index, column, label, "explicit feature absence")
    unsupported = coded & ~supported
    flag(unsupported, "Absence code conflicts with feature information", [column, "Garage Type"])
    for index in df.index[unsupported]:
        change(index, column, pd.NA, "conflicting absence code; feature exists, quality unknown")

# Only fill numeric blanks when absence is supported by the feature columns.
no_basement = df["Bsmt Qual"].eq("No basement") & numeric("Total Bsmt SF").eq(0)
for column in ["Bsmt Full Bath", "Bsmt Half Bath"]:
    for index in df.index[no_basement & df[column].isna()]:
        change(index, column, "0", "zero baths in confirmed absent basement")

future_garage = numeric("Garage Yr Blt") > numeric("Yr Sold")
flag(future_garage, "Garage year after sale year", ["Garage Yr Blt", "Yr Sold"])
invalid_year = numeric("Garage Yr Blt") > 2026
for index in df.index[invalid_year]:
    change(index, "Garage Yr Blt", pd.NA, "invalid year; verified replacement unavailable")
for name, mask, columns in [
    ("Construction after sale", numeric("Year Built") > numeric("Yr Sold"), ["Year Built", "Yr Sold"]),
    ("Remodel after sale", numeric("Year Remod/Add") > numeric("Yr Sold"), ["Year Remod/Add", "Yr Sold"]),
    ("Remodel before construction", numeric("Year Remod/Add") < numeric("Year Built"), ["Year Remod/Add", "Year Built"]),
    ("No masonry type but positive area", df["Mas Vnr Type"].eq("None") & numeric("Mas Vnr Area").gt(0), ["Mas Vnr Type", "Mas Vnr Area"]),
    ("Masonry type but zero area", df["Mas Vnr Type"].notna() & ~df["Mas Vnr Type"].eq("None") & numeric("Mas Vnr Area").eq(0), ["Mas Vnr Type", "Mas Vnr Area"]),
    ("Large living area: review only", numeric("Gr Liv Area") > 4000, ["Gr Liv Area", "SalePrice"]),
]:
    flag(mask, name, columns)

# Restore numeric types; integer measurements use nullable integers.
original_default = pd.read_csv(SOURCE)
numeric_columns = list(original_default.select_dtypes("number").columns)
for column in numeric_columns:
    df[column] = pd.to_numeric(df[column], errors="raise").astype("Int64")
df["MS SubClass"] = df["MS SubClass"].astype("category")
df["PID"] = df["PID"].astype("string")
df["Order"] = df["Order"].astype("string")

assert df.shape == raw.shape
assert df["PID"].is_unique and df["Order"].is_unique
assert df["SalePrice"].notna().all() and df["SalePrice"].gt(0).all()
assert not df["Garage Yr Blt"].gt(2026).any()
assert df["Mas Vnr Type"].eq("None").sum() == 1752
assert all(not isinstance(v, str) or v == v.strip() for c in df for v in df[c])

df.to_csv(OUTPUT, index=False, na_rep="")
review = pd.DataFrame(flags, columns=["Order", "PID", "issue", "source_values"])
review.to_csv(ROOT / "AmesHousing-1-review-flags.csv", index=False)
loaded = pd.read_csv(OUTPUT, keep_default_na=False)
assert loaded.shape == raw.shape
assert loaded["Mas Vnr Type"].eq("None").sum() == 1752
assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == source_hash

report = {
    "source": SOURCE.name, "source_sha256": source_hash,
    "output": OUTPUT.name, "rows": len(df), "columns": len(df.columns),
    "rows_removed": 0, "flagged_records": int(review["Order"].nunique()),
    "flag_counts": review["issue"].value_counts().to_dict(),
    "change_counts": pd.Series([c["reason"] for c in changes]).value_counts().to_dict(),
    "remaining_nulls": {c: int(n) for c, n in df.isna().sum().items() if n},
    "schema": {"categorical": ["MS SubClass"], "identifiers": ["Order", "PID"],
               "nullable_integer_columns": [c for c in numeric_columns if c not in ["Order", "PID", "MS SubClass"]]},
    "loading_note": "CSV does not preserve data types. Use keep_default_na=False, na_values=[''] to preserve literal None; apply schema separately.",
    "policy": "No median/mode imputation or outlier deletion. Missing garage years for absent garages remain not applicable. Other unknowns remain blank. Dates and masonry conflicts retain original values except invalid garage year 2207, which is blanked.",
    "changes": changes,
}
(ROOT / "AmesHousing-1-cleaning-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps({k: v for k, v in report.items() if k not in ["changes", "schema"]}, indent=2))
