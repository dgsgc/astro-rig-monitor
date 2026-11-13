#!/usr/bin/env python3
"""Build a night report page by hand. Same as `python -m arm.report`.

    python3 tools/night_report.py --config config.toml \
        --rig refractor=logs/refractor.jsonl --rig widefield=logs/widefield.jsonl \
        --out night.html
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from arm.report import main  # noqa: E402

if __name__ == "__main__":
    main()
