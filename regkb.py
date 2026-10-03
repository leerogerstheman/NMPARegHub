#!/usr/bin/env python3
"""Entry point for the drug-regulation knowledge base CLI.

Usage:
    python regkb.py crawl
    python regkb.py search 药品注册
    python regkb.py facets
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
