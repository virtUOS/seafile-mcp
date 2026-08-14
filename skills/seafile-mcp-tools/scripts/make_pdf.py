# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "reportlab>=4.0",
# ]
# ///
"""Generate a genuine PDF from a title and plain-text body.

Not part of the deployed package — run standalone with `uv run make_pdf.py`,
which installs its own deps (reportlab) without touching anything else.

The body text is split on blank lines into paragraphs. Upload the result with
seafile_upload_file (base64-encode it first, e.g. with file_to_base64.py) —
never with seafile_write_file, which only ever sends UTF-8 text and will
corrupt binary PDF bytes.

Example:
    uv run make_pdf.py --out /tmp/report.pdf --title "Q3 Report" \\
        --text-file /tmp/body.txt
"""

from __future__ import annotations

import argparse
import sys


def build_pdf(out_path: str, title: str, body: str) -> None:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    def escape(text: str) -> str:
        # Paragraph() interprets its input as a small XML subset, so a title
        # or body containing a literal "<" or "&" (e.g. "a < b", "Q&A",
        # "<draft>") must be escaped or reportlab silently drops the
        # unrecognized "tag" instead of raising — the content just vanishes.
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    styles = getSampleStyleSheet()
    story = [Paragraph(escape(title), styles["Title"]), Spacer(1, 18)]
    for para in body.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        story.append(Paragraph(escape(para).replace("\n", "<br/>"), styles["Normal"]))
        story.append(Spacer(1, 12))

    doc = SimpleDocTemplate(out_path, pagesize=LETTER)
    doc.build(story)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Output .pdf path")
    parser.add_argument("--title", required=True, help="Document title")
    body_group = parser.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--text", help="Body text (paragraphs separated by blank lines)")
    body_group.add_argument("--text-file", help="Path to a file with the body text")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            body = f.read()
    else:
        body = args.text

    build_pdf(args.out, args.title, body)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
