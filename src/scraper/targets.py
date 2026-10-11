"""監視対象機種（初当たりで判定可能な主要機種）の照合。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.normalize import norm_name

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGETS_PATH = PROJECT_ROOT / "config" / "target_machines.json"


@dataclass(frozen=True)
class TargetMachine:
    id: str
    display_name: str
    type: str
    master_ids: tuple[str, ...]
    patterns: tuple[str, ...]
    skip_judgment: bool = False

    @property
    def is_a_type(self) -> bool:
        return self.type == "A"


def load_targets(path: Path = DEFAULT_TARGETS_PATH) -> list[TargetMachine]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    items: list[TargetMachine] = []
    for item in payload.get("machines") or []:
        items.append(
            TargetMachine(
                id=str(item["id"]),
                display_name=str(item.get("display_name") or item["id"]),
                type=str(item.get("type") or "AT"),
                master_ids=tuple(str(a) for a in (item.get("master_ids") or [])),
                patterns=tuple(str(p) for p in (item.get("patterns") or []) if str(p).strip()),
                skip_judgment=bool(item.get("skip_judgment")),
            )
        )
    return items


def match_target(machine_name: str | None, targets: list[TargetMachine] | None = None) -> TargetMachine | None:
    target_text = norm_name(machine_name)
    if not target_text:
        return None
    scored: list[tuple[int, TargetMachine]] = []
    for machine in targets or load_targets():
        for pattern in machine.patterns:
            key = norm_name(pattern)
            if len(key) < 3:
                continue
            if target_text == key:
                scored.append((10000 + len(key), machine))
            elif key in target_text:
                scored.append((len(key), machine))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def is_watched(machine_name: str | None, targets: list[TargetMachine] | None = None) -> bool:
    return match_target(machine_name, targets) is not None


DEFAULT_EXCLUDE_PATTERNS = ("ヴァルヴレイヴ", "ヴヴヴ", "valvrave")


def exclude_patterns(path: Path = DEFAULT_TARGETS_PATH) -> tuple[str, ...]:
    if not path.is_file():
        return DEFAULT_EXCLUDE_PATTERNS
    payload = json.loads(path.read_text(encoding="utf-8"))
    extra = tuple(str(p) for p in (payload.get("exclude_patterns") or []) if str(p).strip())
    return extra or DEFAULT_EXCLUDE_PATTERNS


def is_judgment_excluded(machine_name: str | None, targets: list[TargetMachine] | None = None) -> bool:
    watched = match_target(machine_name, targets)
    if watched and watched.skip_judgment:
        return True
    text = norm_name(machine_name)
    if not text:
        return False
    for pattern in exclude_patterns():
        key = norm_name(pattern)
        if len(key) >= 2 and key in text:
            return True
    return False
