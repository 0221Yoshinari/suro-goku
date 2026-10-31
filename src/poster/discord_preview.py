"""X投稿の前に、文面と画像を Discord へプレビューとして送る。"""

from __future__ import annotations

import json

import httpx

from src.notify.discord import webhook_url

_LABEL = "【X投稿プレビュー】"


class DiscordPreviewError(RuntimeError):
    """プレビューを送れなかった。"""


def should_preview(*, discord: bool, ok: bool, image_ready: bool, text_only: bool = False) -> bool:
    return bool(discord) and bool(ok) and (bool(text_only) or image_ready)


def send_x_preview(
    text: str,
    image: bytes | None = None,
    *,
    images: list[bytes] | None = None,
    label: str = _LABEL,
    client: httpx.Client | None = None,
) -> int:
    """本文を送る。画像は1枚または複数枚。Webhook URL はエラー文に出さない。"""
    url = webhook_url()
    if not url:
        raise DiscordPreviewError("DISCORD_WEBHOOK_URL が未設定です")
    if not text.strip():
        raise DiscordPreviewError("プレビューする文面が空です")
    pictures = _preview_images(image, images)
    content = f"{label}\n{text.strip()}"
    if len(content) > 1900:
        content = content[:1899] + "…"
    own_client = client is None
    http = client or httpx.Client(timeout=30)
    try:
        try:
            if not pictures:
                response = http.post(url, json={"content": content})
            else:
                payload = json.dumps({"content": content}, ensure_ascii=False)
                files = {
                    f"files[{index}]": (f"image-{index + 1}.png", blob, "image/png")
                    for index, blob in enumerate(pictures[:10])
                }
                response = http.post(
                    url,
                    data={"payload_json": payload},
                    files=files,
                )
        except httpx.HTTPError as exc:
            raise DiscordPreviewError("Discordへのプレビュー送信に失敗しました (接続エラー)") from exc
    finally:
        if own_client:
            http.close()
    if response.status_code >= 400:
        raise DiscordPreviewError(f"Discordへのプレビュー送信に失敗しました ({response.status_code})")
    return int(response.status_code)


def _preview_images(image: bytes | None, images: list[bytes] | None) -> list[bytes]:
    if images:
        found = [item for item in images if item]
    elif image:
        found = [image] if image else []
    else:
        found = []
    if image is not None and not image and not images:
        raise DiscordPreviewError("プレビューする画像が空です")
    for blob in found:
        if not blob:
            raise DiscordPreviewError("プレビューする画像が空です")
    return found
