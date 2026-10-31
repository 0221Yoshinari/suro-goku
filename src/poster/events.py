"""その日のイベントを、判定データのホール名へ結びつける。"""

from __future__ import annotations

from src.poster.labels import display_name


def resolve_events(rows: list[dict], date: str, halls: list | None = None) -> list[dict]:
    """イベントを {event_type, event_title, hall_names, date} に揃える。

    hall_names が既にある行は、ホール台帳を読まずに使う。
    系列イベントは対象県があるとき、その県の店舗だけに広げる。
    """
    ready: list[dict] = []
    pending: list[dict] = []
    for raw in rows:
        if not isinstance(raw, dict) or str(raw.get("date") or "") != date:
            continue
        names = raw.get("hall_names")
        if isinstance(names, list) and any(str(name).strip() for name in names):
            ready.append(_public_row(raw, date, [str(name) for name in names if str(name).strip()]))
        else:
            pending.append(raw)
    if not pending:
        return ready
    if halls is None:
        from src.scraper.halls import load_halls

        halls = load_halls(include_disabled=True)
    by_id = {hall.id: hall for hall in halls}
    by_group: dict[str, list] = {}
    for hall in halls:
        by_group.setdefault(hall.group or "", []).append(hall)
    for raw in pending:
        names: list[str] = []
        group_id = str(raw.get("group") or "")
        prefectures = {str(item).strip() for item in (raw.get("prefectures") or []) if str(item).strip()}
        if group_id:
            for hall in by_group.get(group_id, []):
                if prefectures and hall.prefecture not in prefectures:
                    continue
                _add_hall_labels(names, hall)
        hall = by_id.get(str(raw.get("hall_id") or ""))
        if hall is not None:
            _add_hall_labels(names, hall)
        explicit = str(raw.get("hall_name") or "").strip()
        if explicit and explicit not in names:
            names.append(explicit)
        if names:
            ready.append(_public_row(raw, date, names))
    return ready


def _public_row(raw: dict, date: str, names: list[str]) -> dict:
    return {
        "date": date,
        "event_type": str(raw.get("event_type") or ""),
        "event_title": display_name(raw.get("event_title")),
        "hall_names": names,
    }


def _add_hall_labels(names: list[str], hall) -> None:
    for label in (hall.name, *hall.aliases):
        text = str(label or "").strip()
        if text and text not in names:
            names.append(text)
