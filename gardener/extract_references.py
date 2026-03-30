#!/usr/bin/env python3
"""
Extract text from ebooks (PDF, EPUB) into plain text files for use as
reference material during synthetic data generation.

Usage:
    python extract_references.py /path/to/ebooks/ /path/to/output_texts/

Supports: .pdf, .epub, .txt (copied as-is)
"""

import argparse
import sys
from pathlib import Path


def extract_pdf(pdf_path: Path) -> str:
    """Extract text from a PDF file."""
    try:
        import pymupdf  # PyMuPDF / fitz
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            print(f"  Warning: PyMuPDF not installed. pip install pymupdf")
            return ""

    text_parts = []
    doc = pymupdf.open(str(pdf_path))
    for page_num, page in enumerate(doc):
        text = page.get_text()
        if text.strip():
            text_parts.append(f"--- Page {page_num + 1} ---\n{text}")
    doc.close()
    return "\n\n".join(text_parts)


def extract_epub(epub_path: Path) -> str:
    """Extract text from an EPUB file."""
    try:
        import ebooklib
        from ebooklib import epub
        from bs4 import BeautifulSoup
    except ImportError:
        print(f"  Warning: ebooklib/beautifulsoup4 not installed.")
        print(f"  pip install ebooklib beautifulsoup4")
        return ""

    book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
    text_parts = []
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_content(), "html.parser")
            text = soup.get_text(separator="\n")
            if text.strip():
                text_parts.append(text)
    return "\n\n".join(text_parts)


def main():
    parser = argparse.ArgumentParser(description="Extract text from ebooks")
    parser.add_argument("input_dir", help="Directory containing ebook files")
    parser.add_argument("output_dir", help="Directory to write extracted text files")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.exists():
        print(f"Error: {input_dir} does not exist")
        sys.exit(1)

    extractors = {
        ".pdf": extract_pdf,
        ".epub": extract_epub,
    }

    files = sorted(
        f for f in input_dir.iterdir()
        if f.suffix.lower() in extractors or f.suffix.lower() == ".txt"
    )

    if not files:
        print(f"No supported files found in {input_dir}")
        print(f"Supported formats: .pdf, .epub, .txt")
        sys.exit(1)

    print(f"Found {len(files)} files to process")

    for f in files:
        print(f"  Processing: {f.name}...", end=" ", flush=True)
        out_path = output_dir / f"{f.stem}.txt"

        if f.suffix.lower() == ".txt":
            out_path.write_text(f.read_text(errors="replace"))
            print("copied")
            continue

        extractor = extractors.get(f.suffix.lower())
        if not extractor:
            print("skipped (unsupported)")
            continue

        text = extractor(f)
        if text:
            out_path.write_text(text)
            word_count = len(text.split())
            print(f"done ({word_count:,} words)")
        else:
            print("failed (no text extracted)")

    print(f"\nExtracted texts saved to {output_dir}/")
    print("Use with: python generate.py --references", str(output_dir))


if __name__ == "__main__":
    main()
