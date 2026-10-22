import hashlib
import json
import urllib.request
import zipfile
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "data" / "raw"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
UCI_URL = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
ZIP_PATH = RAW_DIR / "online_retail_ii.zip"
XLSX_PATH = RAW_DIR / "online_retail_II.xlsx"
RAW_PARQUET_PATH = PROCESSED_DIR / "raw_transactions.parquet"
METADATA_PATH = PROCESSED_DIR / "ingestion_metadata.json"

EXPECTED_XLSX_SHA256 = "bcbe73b35f5b7babf197fb0cb983a11f5d9ff929078d4aa53d171b1f2df2e980"
VERIFY_STRICT = True  # Locked to the verified UCI Online Retail II workbook hash


def compute_sha256(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def download_and_extract() -> str:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    if not XLSX_PATH.exists():
        if not ZIP_PATH.exists():
            print(f"Downloading UCI dataset from {UCI_URL}...")
            urllib.request.urlretrieve(UCI_URL, ZIP_PATH)
        print("Extracting workbook...")
        with zipfile.ZipFile(ZIP_PATH, "r") as z:
            z.extractall(RAW_DIR)

    actual_hash = compute_sha256(XLSX_PATH)
    if VERIFY_STRICT and actual_hash != EXPECTED_XLSX_SHA256:
        raise ValueError(
            f"PROVENANCE FAILURE: SHA-256 mismatch!\n"
            f"Expected: {EXPECTED_XLSX_SHA256}\n"
            f"Actual:   {actual_hash}"
        )
    return actual_hash


def ingest_workbook(xlsx_hash: str) -> None:
    print("Reading Excel sheets (Year 2009-2010 and Year 2010-2011)...")
    sheets = ["Year 2009-2010", "Year 2010-2011"]
    frames = []
    sheet_counts = {}

    for idx, sheet in enumerate(sheets):
        df = pd.read_excel(
            XLSX_PATH,
            sheet_name=sheet,
            dtype={
                "Invoice": str,
                "StockCode": str,
                "Description": str,
                "Quantity": "int64",
                "Price": "float64",
                "Customer ID": "float64",
                "Country": str,
            },
        )
        df["source_sheet"] = sheet
        df["raw_row_id"] = [f"{idx}_{i}" for i in range(len(df))]
        sheet_counts[sheet] = int(len(df))
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True)
    combined.rename(
        columns={
            "Invoice": "invoice",
            "StockCode": "stock_code",
            "Description": "description",
            "Quantity": "quantity",
            "InvoiceDate": "invoice_date",
            "Price": "price",
            "Customer ID": "customer_id",
            "Country": "country",
        },
        inplace=True,
    )

    combined["invoice_date"] = pd.to_datetime(combined["invoice_date"])
    combined.sort_values(by=["raw_row_id"], inplace=True)
    combined.to_parquet(RAW_PARQUET_PATH, index=False)

    metadata = {
        "dataset": "UCI Online Retail II",
        "author": "Daqing Chen",
        "license": "CC BY 4.0",
        "xlsx_sha256": xlsx_hash,
        "sheet_row_counts": sheet_counts,
        "total_raw_rows": int(len(combined)),
    }
    with open(METADATA_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(metadata, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"Ingestion complete. Total raw rows: {len(combined):,}")


if __name__ == "__main__":
    file_hash = download_and_extract()
    ingest_workbook(file_hash)