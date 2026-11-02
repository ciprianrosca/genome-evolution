#!/usr/bin/env python3
"""A stand-in for the real resume renderer, for the resume-tailoring example's test runs.

For every Markdown file named on the command line, it writes a placeholder .docx and .pdf file next to it (same
name, other extension) and exits 0. It never reads the Markdown, so a run doesn't depend on a real renderer."""
import os
import sys

for arg in sys.argv[1:]:
    if arg.endswith(".md") and os.path.isfile(arg):
        stem = arg[:-3]
        for ext in (".docx", ".pdf"):
            with open(stem + ext, "w", encoding="utf-8") as f:
                f.write(f"Placeholder {ext[1:]} rendered from {os.path.basename(arg)} (a stand-in, not a real file).\n")
        print(f"Rendered {arg} to {stem}.docx and {stem}.pdf (placeholders).")
sys.exit(0)
