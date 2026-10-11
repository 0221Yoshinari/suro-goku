"""外部通知。"""

from src.notify.discord import notify_error, notify_success, send_test

__all__ = ["notify_error", "notify_success", "send_test"]
