"""Telegram alerting: message formatting, retries and proxy support.

Alerts are sent with the plain Bot API (``sendMessage``) over ``requests`` so we
can route them through an HTTP or SOCKS5 proxy — from inside Iran, reaching
``api.telegram.org`` usually needs one (``TELEGRAM_PROXY=socks5h://127.0.0.1:10808``).

Formatting uses MarkdownV2 (bold labels, clickable link) and the required
inline keyboard button ``[مشاهده آگهی در دیوار]``. MarkdownV2 is strict about
escaping, so every message is escaped by :func:`escape_markdown`; if Telegram
still rejects a payload we transparently re-send it as plain text, because a
plain alert always beats a lost one.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

import requests

from config import Settings
from filters import Verdict
from persian import (
    format_datetime_fa,
    format_number_fa,
    human_toman,
    relative_time_fa,
)

__all__ = [
    "BaseNotifier",
    "TelegramNotifier",
    "DryRunNotifier",
    "TelegramError",
    "build_notifier",
    "format_alert",
    "build_alert_text",
    "build_summary_message",
    "escape_markdown",
    "CATEGORY_NAMES_FA",
]

LOGGER = logging.getLogger("divar.notifier")

MAX_MESSAGE_LENGTH = 4096  # Telegram hard limit for sendMessage text
#: Telegram's ``retry_after`` is authoritative, so it gets its own cap instead of
#: being squeezed into BACKOFF_MAX (which tunes *our* exponential backoff).
RETRY_AFTER_CAP = 60.0

#: Persian labels for the Divar category slugs we ship aliases for.
CATEGORY_NAMES_FA: Dict[str, str] = {
    "mobile-phones": "موبایل",
    "mobile-tablet": "موبایل و تبلت",
    "tablet": "تبلت",
    "sim-card": "سیم‌کارت",
    "mobile-tablet-accessories": "لوازم جانبی موبایل و تبلت",
    "electronic-devices": "کالای دیجیتال",
    "audio-video": "صوتی و تصویری",
    "tv-projector": "تلویزیون و پروژکتور",
    "camera-camcoders": "دوربین عکاسی و فیلم‌برداری",
    "game-consoles-and-video-games": "کنسول و بازی ویدئویی",
    "appliance": "لوازم خانگی برقی",
    "home-kitchen": "خانه و آشپزخانه",
    "cars": "خودرو",
    "vehicles": "وسایل نقلیه",
    "real-estate": "املاک",
    "personal": "وسایل شخصی",
    "leisure-hobbies": "سرگرمی و فراغت",
    "services": "خدمات",
    "jobs": "استخدام و کاریابی",
    "tools-materials-equipment": "تجهیزات و صنعتی",
}

_MD_ESCAPES = r"_*[]()~`>#+-=|{}.!"


class TelegramError(RuntimeError):
    """Telegram refused a call. ``retryable`` drives the retry decision."""

    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = False,
                 retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


def escape_markdown(text: object) -> str:
    """Escape every MarkdownV2 special character in ``text``."""
    if text is None:
        return ""
    out = []
    for char in str(text):
        if char in _MD_ESCAPES:
            out.append("\\")
        out.append(char)
    return "".join(out)


def _truncate(text: str, limit: int = 300, suffix: str = "…") -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + suffix


# --------------------------------------------------------------------------- #
# message building (pure functions — easy to snapshot in tests)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AlertMessage:
    """A ready-to-send alert."""

    markdown: str
    plain: str
    url: str
    button_text: str = "مشاهده آگهی در دیوار"

    @property
    def inline_keyboard(self) -> Dict[str, Any]:
        return {
            "inline_keyboard": [[{"text": self.button_text, "url": self.url}]]
        } if self.url else {"inline_keyboard": []}


def _category_fa(slug: Optional[str]) -> str:
    if not slug:
        return "نامشخص"
    return CATEGORY_NAMES_FA.get(slug, slug)


def build_alert_text(
    verdict: Verdict,
    settings: Settings,
    *,
    now: Optional[datetime] = None,
    headline: str = "🔥 شکار معامله در دیوار",
) -> AlertMessage:
    """Render one deal as MarkdownV2 plus a plain-text twin.

    Fields, in the order they read on a phone::

        📌 عنوان آگهی   💰 قیمت اعلامی   📍 محل / محله
        🕒 زمان انتشار  🏷 دسته‌بندی      📉 اختلاف با سقف
        ✅ چرا معامله است   ⚠️ هشدار   📝 توضیحات   🔗 لینک مستقیم

    plus the inline keyboard button ``مشاهده آگهی در دیوار``.
    """
    post = verdict.post
    price = verdict.price
    now = now or datetime.now(timezone.utc)

    published = post.published_at
    if published is not None and published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)

    time_fa = format_datetime_fa(published, settings.tz_offset_hours) if published else "نامشخص"
    if published is not None and not post.time_is_exact and post.time_text:
        time_fa = f"{time_fa} (تقریبی — دیوار: {post.time_text})"
    if published is not None:
        time_fa = f"{time_fa} • {relative_time_fa(published, now)}"

    rows: List[tuple] = [
        ("📌 عنوان آگهی", _truncate(post.title or "(بدون عنوان)", 160)),
        ("💰 قیمت اعلامی", price.display),
        ("📍 محل / محله", post.location() or "نامشخص"),
        ("🕒 زمان انتشار", time_fa),
        ("🏷 دسته‌بندی", _category_fa(post.category)),
    ]

    if price.value is not None and settings.deal_max_price:
        saved = settings.deal_max_price - price.value
        if saved > 0:
            rows.append(
                (
                    "📉 اختلاف با سقف",
                    f"{human_toman(saved)} ({format_number_fa(verdict.score)}٪ ارزان‌تر از سقف)",
                )
            )
    if post.brand_model:
        rows.append(("🔖 مدل", _truncate(post.brand_model, 80)))
    if post.condition:
        condition_fa = {"used": "دست دوم", "new": "نو", "rent": "اجاره"}.get(
            post.condition, post.condition
        )
        rows.append(("🧾 وضعیت", condition_fa))

    reasons = [_truncate(line, 200) for line in verdict.reasons] or ["زیر سقف قیمت تعیین‌شده"]
    warnings = [_truncate(line, 200) for line in verdict.warnings]
    description = _truncate(post.description, 220) if post.description else ""

    def render(markdown: bool) -> str:
        esc = escape_markdown if markdown else (lambda value: str(value if value is not None else ""))
        bold = (lambda text: f"*{escape_markdown(text)}*") if markdown else (lambda text: text)

        out: List[str] = [bold(headline), ""]
        out.extend(f"{bold(label + ':')} {esc(value)}" for label, value in rows)
        out.append("")
        out.append(bold("✅ چرا معامله است:"))
        out.extend(f"  • {esc(reason)}" for reason in reasons)
        if warnings:
            out.append(bold("⚠️ هشدار:"))
            out.extend(f"  • {esc(warning)}" for warning in warnings)
        if description:
            out.append("")
            out.append(f"📝 {esc(description)}")
        out.append("")
        out.append(f"{bold('🔗 لینک مستقیم:')} {esc(post.url)}")
        return "\n".join(out)[:MAX_MESSAGE_LENGTH]

    return AlertMessage(markdown=render(True), plain=render(False), url=post.url)


def format_alert(verdict: Verdict, settings: Settings, **kwargs: Any) -> AlertMessage:
    """Public alias of :func:`build_alert_text`."""
    return build_alert_text(verdict, settings, **kwargs)


def build_summary_message(
    verdicts: Sequence[Verdict],
    settings: Settings,
    headline: str = "📦 سایر معاملات این چرخه",
    markdown: bool = True,
) -> str:
    """Compact list for deals that did not fit in the per-cycle alert budget."""
    esc = escape_markdown if markdown else (lambda value: str(value))
    bold = (lambda text: f"*{escape_markdown(text)}*") if markdown else (lambda text: text)
    lines = [bold(headline), ""]
    for verdict in verdicts:
        price = (
            human_toman(verdict.price.value) if verdict.price.value is not None else "بدون قیمت"
        )
        title = _truncate(verdict.post.title or "(بدون عنوان)", 60)
        if markdown:
            lines.append(f"• [{esc(title)}]({esc(verdict.post.url)}) — {esc(price)}")
        else:
            lines.append(f"• {esc(title)} — {esc(price)}\n  {esc(verdict.post.url)}")
    return "\n".join(lines)[:MAX_MESSAGE_LENGTH]


# --------------------------------------------------------------------------- #
# notifiers
# --------------------------------------------------------------------------- #
class BaseNotifier:
    """Interface the monitoring loop talks to."""

    def alert(self, verdict: Verdict, settings: Settings, **kwargs: Any) -> bool:
        raise NotImplementedError

    def send_text(self, text: str, **kwargs: Any) -> bool:
        raise NotImplementedError

    def summary(self, verdicts: Sequence[Verdict], settings: Settings) -> bool:
        """Send the overflow list for deals beyond the per-cycle alert budget."""
        if not verdicts:
            return True
        return self.send_text(build_summary_message(verdicts, settings, markdown=False))

    def verify(self) -> str:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - default is a no-op
        return None


class DryRunNotifier(BaseNotifier):
    """Logs alerts instead of sending them (``DRY_RUN=true``, tests, demos)."""

    def __init__(self, settings: Settings, logger: Optional[logging.Logger] = None) -> None:
        self.settings = settings
        self.log = logger or LOGGER
        self.sent: List[str] = []

    def alert(self, verdict: Verdict, settings: Settings, **kwargs: Any) -> bool:
        message = build_alert_text(verdict, settings, **kwargs)
        self.sent.append(message.plain)
        self.log.info("DRY-RUN alert for %s:\n%s", verdict.post.token, message.plain)
        return True

    def send_text(self, text: str, **kwargs: Any) -> bool:
        self.sent.append(text)
        self.log.info("DRY-RUN message:\n%s", text)
        return True

    def verify(self) -> str:
        return "dry-run notifier active (nothing will be sent to Telegram)"


class TelegramNotifier(BaseNotifier):
    """Bot API sender with proxy support, retries and format fallbacks."""

    def __init__(
        self,
        settings: Settings,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Optional[random.Random] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.settings = settings
        self.token = settings.telegram_bot_token
        self.chat_ids = _split_chat_ids(settings.telegram_chat_id)
        self.session = session or requests.Session()
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._should_stop = should_stop or (lambda: False)
        self._last_call = 0.0
        self._lock = threading.Lock()
        self.stats: Dict[str, int] = {"sent": 0, "failed": 0, "retries": 0, "plain_fallback": 0}

    # ------------------------------------------------------------------ #
    @property
    def api_url(self) -> str:
        return f"{self.settings.telegram_api_base.rstrip('/')}/bot{self.token}"

    def _proxies(self) -> Optional[Dict[str, str]]:
        return self.settings.proxies_for("telegram")

    def _throttle(self) -> None:
        """Keep at least ``ALERT_SPACING_SECONDS`` between two calls."""
        spacing = self.settings.alert_spacing_seconds
        if spacing <= 0:
            return
        with self._lock:
            wait = spacing - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            self._last_call = time.monotonic()

    def call(self, method: str, payload: Dict[str, Any]) -> Any:
        """One Bot API call with retries. Returns ``result`` or raises."""
        url = f"{self.api_url}/{method}"
        attempts = max(1, self.settings.telegram_max_retries)
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            if self._should_stop():
                raise TelegramError("shutdown requested before Telegram call", retryable=False)
            self._throttle()
            try:
                response = self.session.post(
                    url,
                    json=payload,
                    timeout=self.settings.telegram_timeout,
                    proxies=self._proxies(),
                )
            except requests.RequestException as exc:
                last_error = exc
                LOGGER.warning(
                    "Telegram %s failed (attempt %d/%d): %s", method, attempt + 1, attempts, exc
                )
            else:
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                if data.get("ok"):
                    self.stats["sent"] += 1
                    return data.get("result")

                status = int(data.get("error_code") or response.status_code or 0)
                description = str(data.get("description") or response.text[:200])
                retry_after = (data.get("parameters") or {}).get("retry_after")
                retryable = status == 429 or status >= 500

                if status == 400 and "message is not modified" in description:
                    LOGGER.debug("Telegram: message is not modified — ignoring")
                    return None

                error = TelegramError(
                    f"Telegram {method} failed: HTTP {status} {description}",
                    status=status,
                    retryable=retryable,
                    retry_after=float(retry_after) if retry_after is not None else None,
                )
                if not retryable:
                    # 401 bad token, 403 bot blocked, 404 wrong chat id: the
                    # operator has to fix configuration, retrying cannot help.
                    self.stats["failed"] += 1
                    raise error
                last_error = error
                LOGGER.warning("%s (attempt %d/%d)", error, attempt + 1, attempts)

            if attempt == attempts - 1:
                break
            self.stats["retries"] += 1
            delay = self._retry_delay(attempt, getattr(last_error, "retry_after", None))
            self._sleep(delay)

        self.stats["failed"] += 1
        raise TelegramError(f"Telegram {method} gave up after {attempts} attempts: {last_error}")

    def _retry_delay(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after:
            return min(float(retry_after), RETRY_AFTER_CAP)
        delay = self.settings.backoff_base * (2 ** attempt)
        return min(delay, self.settings.backoff_max) + self._rng.uniform(0, 0.75)

    # ------------------------------------------------------------------ #
    def send_message(
        self,
        text: str,
        *,
        chat_ids: Optional[Sequence[str]] = None,
        parse_mode: Optional[str] = "MarkdownV2",
        reply_markup: Optional[Dict[str, Any]] = None,
        disable_web_page_preview: bool = True,
    ) -> bool:
        """Send ``text`` to every configured chat. Returns True if all succeeded."""
        targets = list(chat_ids or self.chat_ids)
        if not targets:
            raise TelegramError("no TELEGRAM_CHAT_ID configured", retryable=False)

        ok = True
        for chat_id in targets:
            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": disable_web_page_preview,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_markup:
                payload["reply_markup"] = reply_markup
            try:
                self.call("sendMessage", payload)
            except TelegramError as exc:
                if parse_mode and exc.status == 400 and _is_parse_error(str(exc)):
                    # MarkdownV2 rejected our payload: fall back to plain text so
                    # the deal is never lost to a formatting quirk.
                    LOGGER.warning("Telegram rejected %s (%s) — resending as plain text",
                                   parse_mode, exc)
                    self.stats["plain_fallback"] += 1
                    plain_payload = dict(payload)
                    plain_payload.pop("parse_mode", None)
                    try:
                        self.call("sendMessage", plain_payload)
                        continue
                    except TelegramError as second:
                        LOGGER.error("plain-text resend also failed: %s", second)
                        ok = False
                        continue
                LOGGER.error("could not deliver alert to %s: %s", chat_id, exc)
                ok = False
        return ok

    def send_text(self, text: str, **kwargs: Any) -> bool:
        return self.send_message(text, **kwargs)

    def alert(self, verdict: Verdict, settings: Settings, **kwargs: Any) -> bool:
        """Send one deal alert. Falls back from MarkdownV2 to plain text."""
        message = build_alert_text(verdict, settings, **kwargs)
        return self.send_message(
            message.markdown,
            parse_mode="MarkdownV2",
            reply_markup=message.inline_keyboard,
        )

    def summary(self, verdicts: Sequence[Verdict], settings: Settings) -> bool:
        if not verdicts:
            return True
        return self.send_message(build_summary_message(verdicts, settings, markdown=True))

    # ------------------------------------------------------------------ #
    def verify(self) -> str:
        """Check the token with ``getMe`` and report on each configured chat."""
        me = self.call("getMe", {})
        identity = f"@{me.get('username')}" if isinstance(me, dict) else "unknown bot"
        notes: List[str] = []
        for chat_id in self.chat_ids:
            try:
                chat = self.call("getChat", {"chat_id": chat_id})
                title = (chat or {}).get("title") or (chat or {}).get("username") or "private chat"
                notes.append(f"chat {chat_id}: reachable ({title})")
            except TelegramError as exc:
                if exc.status in (401, 403):
                    raise
                # A private chat the bot has never been introduced to cannot be
                # queried; that is fixable by pressing /start, not by retrying.
                notes.append(f"chat {chat_id}: NOT reachable ({exc}) — press /start on the bot")
        report = f"Telegram OK — bot {identity}\n" + "\n".join(notes)
        if any("NOT reachable" in note for note in notes):
            raise TelegramError(report, retryable=False)
        return report

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover - closing must never raise
            pass


def _is_parse_error(description: str) -> bool:
    markers = (
        "can't parse entities",
        "cant parse entities",
        "parse entities",
        "markdown",
        "unsupported parse_mode",
        "invalid format",
    )
    low = description.lower()
    return any(marker in low for marker in markers)


def _split_chat_ids(raw: str) -> List[str]:
    """Allow a comma separated list of chat ids / channel handles."""
    if not raw:
        return []
    return [part.strip() for part in raw.replace("،", ",").split(",") if part.strip()]


def build_notifier(
    settings: Settings,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Optional[Callable[[], bool]] = None,
) -> BaseNotifier:
    """Return a dry-run notifier when configured (or unconfigured), else Telegram."""
    if settings.dry_run:
        return DryRunNotifier(settings)
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        LOGGER.warning(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set — falling back to dry-run output"
        )
        return DryRunNotifier(settings)
    return TelegramNotifier(settings, session=session, sleep=sleep, should_stop=should_stop)
