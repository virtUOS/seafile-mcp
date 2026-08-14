"""Base64-encode a local file for use as seafile_upload_file's content_base64.

No third-party dependencies — plain `python3 file_to_base64.py ...` is fine,
`uv run` is not required for this one.

By default prints the base64 text to stdout with no trailing newline, so it
can be captured straight into a variable. Pass --out to write it to a file
instead (useful for large files you don't want dumped into a terminal/log).

Example:
    uv run make_pdf.py --out /tmp/report.pdf --title X --text "..."
    python3 file_to_base64.py /tmp/report.pdf --out /tmp/report.pdf.b64
"""

from __future__ import annotations

import argparse
import base64
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="File to encode")
    parser.add_argument(
        "--out", help="Write base64 text here instead of printing to stdout"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with open(args.path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")

    if args.out:
        with open(args.out, "w", encoding="ascii") as f:
            f.write(encoded)
        print(f"wrote {args.out} ({len(encoded)} base64 chars)", file=sys.stderr)
    else:
        sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
