#!/usr/bin/env python3
"""標準 Chrome の Cookie をテラモバ2取得用スナップショットへ取り込む。

Playwright では Turnstile を通さない。普段の Chrome で同意した Cookie を読む。
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "import_chrome_teramoba_session.py"
    sys.argv = [str(target)]
    runpy.run_path(str(target), run_name="__main__")
