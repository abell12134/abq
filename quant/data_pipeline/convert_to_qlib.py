#!/usr/bin/env python3
"""Convert baostock CSV files to qlib bin format."""
import struct
from pathlib import Path
import pandas as pd

CSV_DIR = Path.home() / "abq/quant/data/csv_raw/history"
QLIB_DIR = Path.home() / ".qlib/qlib_data/cn_data"
CALENDAR_FILE = QLIB_DIR / "calendars/day.txt"
INSTRUMENTS_DIR = QLIB_DIR / "instruments"
FEATURES_DIR = QLIB_DIR / "features"

# qlib bin format: float32 values
# Fields: open, close, high, low, volume, factor
FIELDS = ["open", "close", "high", "low", "volume", "factor"]

def convert_csv_to_bin(csv_path: Path, symbol: str):
    """Convert a single CSV to qlib bin files."""
    df = pd.read_csv(csv_path)
    
    # Rename columns to match qlib format
    df = df.rename(columns={
        "date": "datetime",
        "code": "symbol",
        "turn": "turn",
        "tradestatus": "tradestatus"
    })
    
    # Sort by date
    df = df.sort_values("datetime")
    
    # Create feature directory for this symbol
    sym_dir = FEATURES_DIR / symbol
    sym_dir.mkdir(parents=True, exist_ok=True)
    
    # Write each field as a bin file
    for field in FIELDS:
        if field == "factor":
            # Calculate factor from hfq_close / close
            if "hfq_close" in df.columns and "close" in df.columns:
                values = (df["hfq_close"] / df["close"]).astype("float32").values
            else:
                values = pd.Series([1.0] * len(df), dtype="float32").values
        elif field == "volume":
            values = df["volume"].astype("float32").values
        else:
            values = df[field].astype("float32").values
        
        # Write bin file
        bin_path = sym_dir / f"{field}.day.bin"
        with open(bin_path, "wb") as f:
            # Write start date index (YYYYMMDD format)
            start_date = int(df["datetime"].iloc[0].replace("-", ""))
            f.write(struct.pack("<I", start_date))
            
            # Write number of records
            n_records = len(values)
            f.write(struct.pack("<I", n_records))
            
            # Write data as float32
            for v in values:
                f.write(struct.pack("<f", v))
    
    return len(df)

def main():
    print("Converting CSV to qlib bin format...")
    
    # Create directories
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    INSTRUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Get all CSV files
    csv_files = sorted(CSV_DIR.glob("*.csv"))
    print(f"Found {len(csv_files)} CSV files")
    
    # Collect all dates for calendar
    all_dates = set()
    symbols = []
    
    for csv_file in csv_files:
        symbol = csv_file.stem  # e.g., SH600000
        n_rows = convert_csv_to_bin(csv_file, symbol)
        symbols.append(symbol)
        
        # Collect dates
        df = pd.read_csv(csv_file)
        all_dates.update(df["date"].tolist())
        
        print(f"  {symbol}: {n_rows} rows")
    
    # Write calendar
    sorted_dates = sorted(all_dates)
    CALENDAR_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CALENDAR_FILE, "w") as f:
        for date in sorted_dates:
            f.write(date + "\n")
    print(f"Calendar: {len(sorted_dates)} dates")
    
    # Write instruments file (csi300)
    inst_file = INSTRUMENTS_DIR / "csi300.txt"
    with open(inst_file, "w") as f:
        for sym in symbols:
            f.write(f"{sym}\t{sorted_dates[0]}\t{sorted_dates[-1]}\n")
    print(f"Instruments: {len(symbols)} symbols")
    
    print("Done!")

if __name__ == "__main__":
    main()
