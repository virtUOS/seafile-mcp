# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "openpyxl>=3.1",
# ]
# ///
"""Generate a genuine Excel (.xlsx) workbook from a CSV file.

Not part of the deployed package — run standalone with `uv run make_xlsx.py`,
which installs its own deps (openpyxl) without touching anything else.

Reads the CSV with Python's csv module (handles quoted fields and embedded
commas correctly — do not hand-split on ","), converts cells that parse as
int/float into real numeric cells so formulas/sums work in Excel, and leaves
everything else as text. Upload the result with seafile_upload_file
(base64-encode it first, e.g. with file_to_base64.py) — never with
seafile_write_file, which only ever sends UTF-8 text and will corrupt binary
.xlsx bytes (an .xlsx is a zip archive, not text).

Example:
    uv run make_xlsx.py --csv /tmp/data.csv --out /tmp/data.xlsx \\
        --sheet-name "Q3 Data"
"""

from __future__ import annotations

import argparse
import csv


def _coerce(value: str):
    if value == "":
        return value
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def build_xlsx(csv_path: str, out_path: str, sheet_name: str) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]  # Excel caps sheet names at 31 chars

    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is not None:
            ws.append(header)
        for row in reader:
            ws.append([_coerce(cell) for cell in row])

    wb.save(out_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Input CSV path")
    parser.add_argument("--out", required=True, help="Output .xlsx path")
    parser.add_argument("--sheet-name", default="Sheet1", help="Worksheet name")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    build_xlsx(args.csv, args.out, args.sheet_name)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
