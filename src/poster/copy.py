"""選ばれたホールから、Xに載せる文面を組み立てる。

数値はスロ極の画面にあるものだけを使う。
絵文字と感嘆符は使わず、文は句点で終える。
本文に外部URLは置かない。公式LINEの返信は付けない。ハッシュタグは付けない。
"""

from __future__ import annotations

from src.dashboard.line_access import line_official_url
from src.poster.chase import ChaseTarget
from src.poster.day_flow import SOFT_DIGIT_TAG
from src.poster.select import HallPick, MachineCard
from src.scraper.halls import PREFECTURE_LABELS, PRIORITY_PREFECTURES

_LIMIT = 280
_CTA = "1週間無料でスロ極を使ってみてください↓"


def line_reply() -> str:
    """公式LINEは本文ではなく、自分の返信に置く。"""
    return f"{_CTA}\n{line_official_url()}"


def line_reply_for(*, enabled: bool) -> str:
    """投稿のスレッドには、無料案内を付けない。"""
    del enabled
    return ""


def line_reply_hall(slot: str, targets: list[ChaseTarget]) -> str:
    """昼と夜で、当日データがある店のうち最も強い1店だけを返す。朝は空。"""
    if slot not in {"midday", "evening"}:
        return ""
    ready = [target for target in targets if target.same_day is not None]
    if not ready:
        return ""
    ready.sort(key=lambda target: (-target.points, -target.avg_games, -target.stability, target.hall_name))
    return ready[0].hall_name


def compose_post(pick: HallPick, *, date: str, slot: str | None = None) -> str:
    del date
    chosen = slot or _slot(_hour(pick))
    if pick.pattern == "digits":
        body = "\n".join(_digit_lines(pick, chosen))
    else:
        body = "\n".join(_hall_lines(pick, chosen, include_second=True))
        if _overflows(body, chosen):
            body = "\n".join(_hall_lines(pick, chosen, include_second=False))
    return _finish(body, chosen)


_DATA_SLOTS = {"pickup", "midday", "afternoon", "evening", "night"}
_MORNING_LEAD = "反応した機種とホールを随時投稿していきます。"


def morning_sections(targets: list[ChaseTarget]) -> list[tuple[str, list[ChaseTarget]]]:
    """イベントがある県だけを並べる。店が無い県は出さない。"""
    buckets: dict[str, list[ChaseTarget]] = {}
    for target in targets:
        buckets.setdefault(target.prefecture or "", []).append(target)
    order = [key for key in _PREFECTURE_ORDER if key in buckets]
    order.extend(sorted(key for key in buckets if key not in _PREFECTURE_ORDER and key))
    if "" in buckets:
        order.append("")
    return [(_prefecture_label(key), buckets[key]) for key in order]


def compose_morning(targets: list[ChaseTarget]) -> str:
    """朝は、その日のイベント店を1投稿にする。数値は書かない。"""
    lines = ["今日追うホール。", "", _MORNING_LEAD, ""]
    for label, rows in morning_sections(targets):
        lines.append(label)
        for target in rows:
            lines.append(f"・{target.hall_name}　{_reason_phrase(target)}")
        lines.append("")
    return _finish("\n".join(lines).strip(), "morning")


def compose_morning_areas() -> str:
    """朝は大阪・東京・福岡の全店を対象に追う宣言。数値は書かない。"""
    from src.poster.day_flow import poster_area_labels

    lines = ["今日追うエリア。", ""]
    for _area, label in poster_area_labels():
        lines.append(f"・{label}")
    lines.extend(["", _MORNING_LEAD])
    return _finish("\n".join(lines).strip(), "morning")


def compose_chase(target: ChaseTarget, slot: str) -> str:
    """数値の投稿。全台系・末尾・並びが無い店は空文字を返し、投稿しない。"""
    if slot not in _DATA_SLOTS or target.same_day is None:
        return ""
    elif target.same_day.pattern == "digits":
        body = "\n".join(_digit_lines(target.same_day, slot))
    else:
        body = "\n".join(_hall_lines(target.same_day, slot, include_second=True))
        if _overflows(body, slot):
            body = "\n".join(_hall_lines(target.same_day, slot, include_second=False))
    if not body.strip():
        return ""
    return _finish(body, slot)


def _overflows(body: str, slot: str) -> bool:
    hook = _hook(slot)
    extra = 2 + len(hook) if hook else 0
    return len(body.strip()) + extra > _LIMIT


def _finish(body: str, slot: str) -> str:
    hook = _hook(slot)
    text = body.strip()
    if not hook:
        if len(text) <= _LIMIT:
            return text
        return text[: _LIMIT - 1].rstrip() + "…"
    joined = f"{text}\n\n{hook}"
    if len(joined) <= _LIMIT:
        return joined
    room = _LIMIT - len(hook) - 2
    if room < 2:
        return hook[:_LIMIT]
    trimmed = text if len(text) <= room else text[: room - 1].rstrip() + "…"
    return f"{trimmed}\n\n{hook}"


def _hook(slot: str) -> str:
    del slot
    return ""


_AREA_LABEL = {"fukuoka": "福岡", "osaka": "大阪", "tokyo": "東京"}
_PREFECTURE_ORDER = PRIORITY_PREFECTURES + tuple(
    key for key, _label in PREFECTURE_LABELS.items() if key not in PRIORITY_PREFECTURES
)


def _prefecture_label(slug: str) -> str:
    if not slug:
        return "その他"
    if slug in _AREA_LABEL:
        return _AREA_LABEL[slug]
    label = PREFECTURE_LABELS.get(slug, slug)
    if label == "北海道":
        return label
    for suffix in ("都", "府", "県"):
        if label.endswith(suffix) and len(label) > len(suffix):
            return label[: -len(suffix)]
    return label


def _reason_phrase(target: ChaseTarget) -> str:
    titles = [title for title in target.event_titles if title]
    if not titles:
        return "イベント加点。"
    if len(titles) == 1:
        return f"{_short(titles[0], 28)}。"
    return f"{_short(titles[0], 18)}、{_short(titles[1], 18)}。"


def _note(pick: HallPick, slot: str) -> str:
    """締めは断定しない。末尾か機種に合わせて、店ごとに言い方を変える。"""
    if pick.pattern == "digits" and pick.digits:
        digit = pick.digits[0].digit
        pool = (
            (
                f"これからお店を決める方は、このお店の末尾{digit}を狙ってもいいかも？",
                f"末尾{digit}で迷ってる方は参考に。",
            )
            if slot not in {"evening", "night"}
            else (
                f"夜から末尾を探す方は、末尾{digit}もアリかも？",
                f"このお店の末尾{digit}、狙ってみるのもアリかも？",
            )
        )
        return _soft_choice(pick.hall_name, pool)
    label = _play_label(pick)
    night = slot in {"evening", "night"}
    if night and label == "ジャグラー打ち":
        pool = (
            "夜からジャグラーを打つか迷ってる方は、この店もアリかも？",
            "ジャグラー打ちで迷ってる方は、参考までに。",
        )
    elif night:
        pool = (
            f"夜から{label}を打つか迷ってる方は、この店もアリかも？",
            f"{label}で迷ってる方は、参考までに。",
        )
    else:
        pool = (
            f"{label}で迷ってる方は参考に。",
            f"{label}を打つか迷ってる方は、この店もアリかも？",
        )
    return _soft_choice(pick.hall_name, pool)


def _play_label(pick: HallPick) -> str:
    if pick.juggler_hot or any(machine.is_juggler or "ジャグラー" in machine.name for machine in pick.machines):
        return "ジャグラー打ち"
    if pick.machines and pick.machines[0].name:
        return pick.machines[0].name
    return "全台系"


def _soft_choice(name: str, pool: tuple[str, ...]) -> str:
    total = 0
    for index, char in enumerate(name):
        total = (total * 33 + ord(char) + index) % 997
    return pool[total % len(pool)]


def _hall_lines(pick: HallPick, slot: str, *, include_second: bool) -> list[str]:
    lines = [f"{_as_of(pick)}", ""]
    if pick.event_titles:
        lines.append(f"今日は{_short(pick.event_titles[0])}。")
        lines.append("")
    if pick.machines:
        lines.append(_machine_line(pick.machines[0], lead=True))
        if include_second and len(pick.machines) > 1:
            lines.append(_machine_line(pick.machines[1], lead=False))
    close = _note(pick, slot)
    if close:
        lines.extend(["", close])
    return lines


def _digit_lines(pick: HallPick, slot: str) -> list[str]:
    if SOFT_DIGIT_TAG in pick.tags:
        return _digit_lines_soft(pick, slot)
    lines = [f"{_as_of(pick)}", ""]
    if pick.event_titles:
        lines.append(f"今日は{_short(pick.event_titles[0])}。")
        lines.append("")
    if not pick.digits:
        return []
    top = pick.digits[0]
    label = top.status_label or "数値"
    count = f"{top.unit_count}台が{label}" if top.unit_count else label
    lines.append(f"末尾{top.digit}が動いてる。平均設定{top.avg_setting:.2f}、{count}。")
    close = _note(pick, slot)
    if close:
        lines.extend(["", close])
    return lines


def _digit_lines_soft(pick: HallPick, slot: str) -> list[str]:
    lines = [f"{_as_of(pick)}", ""]
    if pick.event_titles:
        lines.append(f"今日は{_short(pick.event_titles[0])}。")
        lines.append("")
    if not pick.digits:
        return []
    top = pick.digits[0]
    depth = _sample_depth(top)
    lines.append(
        f"末尾の差で見ると、いまは末尾{top.digit}が相対的に高め。"
        f"平均推測設定{top.avg_setting:.2f}（{top.unit_count}台・{depth}）。"
    )
    if len(pick.digits) > 1:
        other = pick.digits[1]
        gap = top.avg_setting - other.avg_setting
        if gap >= 0.01:
            lines.append(
                f"末尾{other.digit}（{other.avg_setting:.2f}）より+{gap:.2f}。"
                f"当たり末尾の候補として見ておく価値はある。"
            )
    close = _note(pick, slot)
    if close:
        lines.extend(["", close])
    return lines


def _sample_depth(digit) -> str:
    if digit.avg_games is not None and digit.avg_games >= 1000:
        return "サンプルはそこそこ"
    if digit.total_games is not None and digit.total_games >= 3000:
        return "サンプルはそこそこ"
    return "サンプルは浅い"


def _as_of(pick: HallPick) -> str:
    hour = _hour(pick)
    if hour is None:
        return pick.hall_name
    return f"{pick.hall_name}　{hour}時時点"


def _slot(hour: int | None) -> str:
    if hour is None or hour < 12:
        return "morning"
    if hour < 16:
        return "midday"
    return "evening"


def _hour(pick: HallPick) -> int | None:
    label = pick.clock_label or ""
    if label.endswith("時台") and label[:-2].isdigit():
        return int(label[:-2])
    return None


def _machine_line(machine: MachineCard, *, lead: bool) -> str:
    if "並び" in machine.status_label and not (machine.is_all_high or machine.is_high_56 or machine.is_high_base):
        return f"{machine.name}は{machine.status_label}。"
    heat = _heat(machine)
    detail = _detail(machine)
    if lead:
        tail = f"{detail}、{heat}" if detail else heat
        return f"{machine.name}は全台系挙動。{tail}。"
    if detail:
        return f"{machine.name}も{heat}。{detail}。"
    return f"{machine.name}も{heat}。"


def _heat(machine: MachineCard) -> str:
    if machine.is_all_high:
        return "全6数値"
    if machine.is_high_56:
        return "全56数値"
    if machine.status_label:
        return machine.status_label
    return "全台系"


def _detail(machine: MachineCard) -> str:
    parts: list[str] = []
    if machine.unit_count:
        parts.append(f"{machine.unit_count}台")
    odds = _odds(machine.synthetic_probability)
    if odds:
        name = "初当り" if machine.judgment_type == "AT" else "合成"
        parts.append(f"{name}は{odds}")
    return "、".join(parts)


def _odds(value: float | None) -> str:
    if value is None or value <= 0:
        return ""
    rounded = round(float(value), 2)
    text = f"{rounded:.2f}".rstrip("0").rstrip(".")
    return f"1/{text}"


def _short(title: str, limit: int = 36) -> str:
    if len(title) <= limit:
        return title
    return title[: limit - 1] + "…"
