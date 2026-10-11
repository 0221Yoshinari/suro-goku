"""取得処理の手動ロック。

data/ACQUISITION_HALTED がある間は、ALLOW_RUN=1 が無い限り取得を起動しない。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HALT_PATH = PROJECT_ROOT / "data" / "ACQUISITION_HALTED"


class AcquisitionHalted(RuntimeError):
    """安全待機中のため、外部への取得を始めない。"""


def acquisition_allowed() -> bool:
    if os.environ.get("ALLOW_RUN", "").strip() == "1":
        return True
    return not HALT_PATH.is_file()


def require_acquisition(action: str = "データ取得") -> None:
    if acquisition_allowed():
        return
    raise AcquisitionHalted(
        f"{action}は安全待機中です。再開指示があるまで起動しません。"
        "手動で再開する場合は ALLOW_RUN=1 を付けてください。"
    )


def exit_if_halted(action: str = "データ取得") -> None:
    try:
        require_acquisition(action)
    except AcquisitionHalted as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(0) from exc
