"""Divar listing client — fetch, normalize and enrich posts.

Divar has no public API; this module talks to the same private JSON endpoints
the divar.ir web app uses in the browser, with the headers a browser would send:

``POST {api}/v8/postlist/w/search``     current listing search (cursor pagination)
``POST {api}/v8/search/{city}/{cat}``   legacy listing search (fallback)
``GET  {api}/v8/posts-v2/web/{token}``  single post detail (description, exact price)

The raw payloads are widget trees, so every row is normalized into a flat
:class:`DivarPost`. Requests are polite by design: the User-Agent is rotated,
a jittered delay is inserted between requests, and 429/5xx/timeout errors are
retried with exponential backoff instead of crashing the loop.

Note: Divar's listing gateway generally only answers traffic originating inside
Iran, so ``DIVAR_PROXY`` (or the shared ``PROXY_URL``) is often required.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import requests

from config import Settings
from persian import (
    ParsedPrice,
    parse_jalali_datetime,
    parse_price,
    parse_relative_time,
    split_time_and_place,
)

__all__ = [
    "DivarClient",
    "DivarPost",
    "DivarError",
    "ShutdownRequested",
    "SearchResult",
    "CITY_IDS",
    "CITY_SLUGS",
    "CATEGORY_ALIASES",
    "parse_timestamp",
    "resolve_category",
    "list_known_cities",
]

LOGGER = logging.getLogger("divar.client")

API_BASE = "https://api.divar.ir"
WEB_BASE = "https://divar.ir"
MODERN_SEARCH_PATH = "/v8/postlist/w/search"
DETAIL_PATH = "/v8/posts-v2/web/{token}"
LEGACY_SEARCH_PATH = "/v8/search/{city_id}/{category}"
PAGINATION_TYPE = "type.googleapis.com/post_list.PaginationData"

# Status codes worth retrying (rate limits and transient server trouble).
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524})

# --------------------------------------------------------------------------- #
# Static reference data (Divar's own numeric city ids and category slugs)
# --------------------------------------------------------------------------- #
#: Persian city name -> Divar numeric city id (Tehran = 1).
CITY_IDS: Dict[str, str] = {
    "تهران": "1", "کرج": "2", "مشهد": "3", "اصفهان": "4", "تبریز": "5",
    "شیراز": "6", "اهواز": "7", "قم": "8", "کرمانشاه": "9", "ارومیه": "10",
    "زاهدان": "11", "رشت": "12", "کرمان": "13", "همدان": "14", "اراک": "15",
    "یزد": "16", "اردبیل": "17", "بندرعباس": "18", "قزوین": "19", "زنجان": "20",
    "گرگان": "21", "ساری": "22", "دزفول": "23", "آبادان": "24", "بوشهر": "25",
    "بروجرد": "26", "خرم‌آباد": "27", "سنندج": "28", "اسلام‌شهر": "29", "کاشان": "30",
    "نجف‌آباد": "31", "ایلام": "32", "کیش": "33", "بیرجند": "34", "سمنان": "35",
    "شهرکرد": "36", "بندر ماهشهر": "37", "یاسوج": "38", "بجنورد": "39",
    "بهارستان": "92", "شوش": "240", "سبزوار": "316", "مسجد سلیمان": "317",
    "نیشابور": "318", "گلستان": "323", "پردیس": "329", "فولادشهر": "354", "جلفا": "394",
}

#: ASCII slug -> Divar numeric city id. Slug is what appears in divar.ir URLs.
CITY_SLUGS: Dict[str, str] = {
    "tehran": "1", "karaj": "2", "mashhad": "3", "isfahan": "4", "esfahan": "4",
    "tabriz": "5", "shiraz": "6", "ahvaz": "7", "qom": "8", "kermanshah": "9",
    "urmia": "10", "oroomieh": "10", "zahedan": "11", "rasht": "12", "kerman": "13",
    "hamedan": "14", "arak": "15", "yazd": "16", "ardabil": "17", "bandar-abbas": "18",
    "bandarabbas": "18", "qazvin": "19", "zanjan": "20", "gorgan": "21", "sari": "22",
    "dezful": "23", "abadan": "24", "bushehr": "25", "borujerd": "26",
    "khorramabad": "27", "sanandaj": "28", "eslamshahr": "29", "kashan": "30",
    "najafabad": "31", "ilam": "32", "kish": "33", "birjand": "34", "semnan": "35",
    "shahrekord": "36", "mahshahr": "37", "bandar-mahshahr": "37", "yasuj": "38",
    "bojnurd": "39", "baharestan": "92", "shush": "240", "sabzevar": "316",
    "masjed-soleiman": "317", "nishabur": "318", "golestan": "323", "pardis": "329",
    "fuladshahr": "354", "jolfa": "394",
}

#: Friendly alias -> Divar category slug. Anything unknown is passed through as
#: written, so new Divar categories work without a code change.
CATEGORY_ALIASES: Dict[str, str] = {
    "mobile": "mobile-phones", "mobiles": "mobile-phones", "phone": "mobile-phones",
    "phones": "mobile-phones", "موبایل": "mobile-phones", "گوشی": "mobile-phones",
    "tablet": "tablet", "tablets": "tablet", "تبلت": "tablet",
    "mobile-tablet": "mobile-tablet", "digital": "electronic-devices",
    "electronics": "electronic-devices", "کالای دیجیتال": "electronic-devices",
    "لوازم جانبی": "mobile-tablet-accessories",
    "accessories": "mobile-tablet-accessories",
    "sim-card": "sim-card", "سیم‌کارت": "sim-card",
    "audio-video": "audio-video", "tv": "tv-projector", "تلویزیون": "tv-projector",
    "camera": "camera-camcoders", "دوربین": "camera-camcoders",
    "console": "game-consoles-and-video-games", "گیم": "game-consoles-and-video-games",
    "بازی": "game-consoles-and-video-games",
    "appliance": "appliance", "لوازم خانگی": "appliance",
    "home-kitchen": "home-kitchen", "خانه و آشپزخانه": "home-kitchen",
    "vehicles": "vehicles", "خودرو": "cars", "cars": "cars",
    "real-estate": "real-estate", "املاک": "real-estate",
    "jobs": "jobs", "استخدام": "jobs",
    "services": "services", "خدمات": "services",
    "personal": "personal", "وسایل شخصی": "personal",
}

#: Browser profiles: a User-Agent plus the matching client-hint headers, so a
#: rotated UA does not contradict the rest of the request fingerprint.
USER_AGENT_PROFILES: Tuple[Dict[str, str], ...] = (
    {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-platform": '"Windows"',
    },
    {
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Chromium";v="130", "Not?A_Brand";v="99"',
        "sec-ch-ua-platform": '"macOS"',
    },
    {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0"
        ),
        "sec-ch-ua": '"Microsoft Edge";v="129", "Chromium";v="129", "Not=A?Brand";v="8"',
        "sec-ch-ua-platform": '"Windows"',
    },
    {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) "
            "Gecko/20100101 Firefox/132.0"
        ),
    },
    {
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.6 Safari/605.1.15"
        ),
    },
    {
        "user-agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Chromium";v="130", "Not?A_Brand";v="99"',
        "sec-ch-ua-platform": '"Linux"',
    },
    {
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.6723.102 Mobile Safari/537.36"
        ),
        "sec-ch-ua": '"Chromium";v="130", "Not?A_Brand";v="99"',
        "sec-ch-ua-platform": '"Android"',
    },
    {
        "user-agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1"
        ),
    },
)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class DivarError(RuntimeError):
    """Any failure talking to Divar (network, HTTP status or bad payload)."""

    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class ShutdownRequested(DivarError):
    """Raised inside a retry loop when the service was asked to stop."""


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
@dataclass
class DivarPost:
    """One normalized listing."""

    token: str
    title: str = ""
    price: Optional[int] = None
    price_text: str = ""
    price_note: Optional[str] = None
    price_is_down_payment: bool = False
    price_is_installment: bool = False
    price_is_deposit: bool = False
    city: Optional[str] = None
    district: Optional[str] = None
    published_at: Optional[datetime] = None
    time_text: str = ""
    time_is_exact: bool = False
    url: str = ""
    image_url: Optional[str] = None
    image_count: int = 0
    category: Optional[str] = None
    description: Optional[str] = None
    has_chat: Optional[bool] = None
    business_type: Optional[str] = None
    condition: Optional[str] = None
    brand_model: Optional[str] = None
    source_endpoint: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.url and self.token:
            self.url = f"{WEB_BASE}/v/{self.token}"

    # -- derived -------------------------------------------------------- #
    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """How old the listing is in seconds, or ``None`` when unknown."""
        if self.published_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        moment = self.published_at
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (now - moment).total_seconds()

    def searchable_text(self) -> str:
        """Everything a keyword rule should be matched against."""
        return " ".join(part for part in (self.title, self.description or "") if part)

    def location(self) -> str:
        """``"تهران، سعادت‌آباد"`` — city plus district when known."""
        parts = [p for p in (self.city, self.district) if p]
        return "، ".join(parts)

    def apply_price(self, parsed: ParsedPrice) -> None:
        """Adopt a :class:`~persian.ParsedPrice` as this post's price."""
        if parsed.value is not None:
            self.price = parsed.value
        if parsed.text:
            self.price_text = parsed.text
        if parsed.note:
            self.price_note = parsed.note
        self.price_is_down_payment = self.price_is_down_payment or parsed.is_down_payment
        self.price_is_installment = self.price_is_installment or parsed.is_installment
        self.price_is_deposit = self.price_is_deposit or parsed.is_deposit

    def to_dict(self) -> Dict[str, Any]:
        out = {
            key: getattr(self, key)
            for key in (
                "token", "title", "price", "price_text", "price_note", "city", "district",
                "time_text", "url", "image_url", "image_count", "category", "description",
                "has_chat", "business_type", "condition", "brand_model", "source_endpoint",
            )
        }
        out["published_at"] = self.published_at.isoformat() if self.published_at else None
        out["price_is_down_payment"] = self.price_is_down_payment
        out["price_is_installment"] = self.price_is_installment
        return out


@dataclass
class SearchResult:
    """One page of listings plus the pagination cursor for the next one."""

    posts: List[DivarPost]
    cursor: Optional[Dict[str, Any]] = None
    has_next_page: bool = False
    search_id: Optional[str] = None
    endpoint: str = "modern"
    page: int = 1
    headline: str = ""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def parse_timestamp(value: Any, tz_offset_hours: float = 3.5) -> Optional[datetime]:
    """Parse any timestamp form Divar emits into an aware UTC ``datetime``.

    Handles ISO-8601 strings (``2026-09-21T20:36:10.203722Z``), epoch seconds,
    milliseconds and microseconds (``1790022970203722``), and Jalali text
    (``"۳۱ شهریور ۱۴۰۵، ۰۰:۲۰"``).
    """
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if text.isdigit():
            number = float(text)
        else:
            parsed_jalali = parse_jalali_datetime(text, tz_offset_hours)
            if parsed_jalali:
                return parsed_jalali.astimezone(timezone.utc)
            iso = text.replace("Z", "+00:00")
            try:
                moment = datetime.fromisoformat(iso)
            except ValueError:
                for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        moment = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    return None
            return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

    # Heuristic: 10 digits = seconds, 13 = ms, 16 = µs.
    if number > 1e15:
        number /= 1e6
    elif number > 1e12:
        number /= 1e3
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _dig(obj: Any, *paths: str) -> Any:
    """Return the first present value among dotted ``paths`` (or ``None``)."""
    for path in paths:
        node = obj
        found = True
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                found = False
                break
        if found and node not in (None, "", {}):
            return node
    return None


def _clean_cursor(cursor: Dict[str, Any]) -> Dict[str, Any]:
    """Strip per-session fields Divar expects the client not to echo back."""
    cleaned = dict(cursor)
    for key in ("@type", "search_uid", "viewed_tokens"):
        cleaned.pop(key, None)
    return cleaned


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class DivarClient:
    """Polite, resilient client for Divar's listing endpoints."""

    #: backoff sleeps are broken into slices this long so cancellation is prompt
    SLEEP_SLICE = 0.5

    def __init__(
        self,
        settings: Settings,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Optional[random.Random] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._should_stop = should_stop or (lambda: False)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._profile = self._rng.choice(USER_AGENT_PROFILES)
        self._endpoint: Optional[str] = None  # sticky once one is proven to work
        self._last_request_at: Optional[float] = None
        self.category = resolve_category(settings.category)
        self.stats: Dict[str, int] = {
            "requests": 0, "retries": 0, "errors": 0,
            "modern_pages": 0, "legacy_pages": 0, "details": 0,
        }

    # ------------------------------------------------------------------ #
    # request plumbing
    # ------------------------------------------------------------------ #
    def headers(self, json_body: bool = False) -> Dict[str, str]:
        """Browser-like headers; the User-Agent is re-rolled on every call."""
        self._profile = self._rng.choice(USER_AGENT_PROFILES)
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "origin": self.settings.divar_web_base,
            "referer": self.settings.divar_web_base + "/",
            "connection": "keep-alive",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        }
        headers.update(self._profile)
        if json_body:
            headers["content-type"] = "application/json;charset=UTF-8"
        return headers

    def _throttle(self) -> None:
        """Sleep a jittered interval between requests (never before the first)."""
        if self._last_request_at is not None:
            low, high = self.settings.request_delay_min, self.settings.request_delay_max
            self._sleep(self._rng.uniform(low, max(low, high)))
        self._last_request_at = time.monotonic()

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in small slices so a shutdown request is honoured quickly.

        The slice count is derived from the requested duration rather than from
        the clock, which keeps the wait deterministic for injected sleep
        functions (tests) while still checking for cancellation twice a second.
        """
        remaining = max(0.0, float(seconds))
        if remaining <= 0:
            return
        slices = max(1, math.ceil(remaining / self.SLEEP_SLICE))
        for _ in range(slices):
            if self._should_stop():
                raise ShutdownRequested("shutdown requested during backoff")
            self._sleep(min(self.SLEEP_SLICE, remaining))
            remaining -= self.SLEEP_SLICE

    def _backoff_delay(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None:
            return min(float(retry_after), self.settings.backoff_max)
        delay = self.settings.backoff_base * (2 ** attempt)
        return min(delay, self.settings.backoff_max) + self._rng.uniform(0, 1.0)

    def request(
        self, method: str, path: str, body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """One API call with jitter, retries and exponential backoff.

        Raises :class:`DivarError` when every attempt failed. Never raises a
        raw ``requests`` exception.
        """
        if self._should_stop():
            raise ShutdownRequested("shutdown requested before request")

        url = path if path.startswith("http") else self.settings.divar_api_base + path
        proxies = self.settings.proxies_for("divar")
        attempts = self.settings.max_retries
        last_error: Optional[Exception] = None
        last_status: Optional[int] = None

        for attempt in range(attempts):
            self._throttle()
            retry_after: Optional[float] = None
            try:
                response = self.session.request(
                    method,
                    url,
                    json=body,
                    headers=self.headers(json_body=body is not None),
                    timeout=self.settings.request_timeout,
                    proxies=proxies,
                )
            except requests.RequestException as exc:
                # Timeout, DNS failure, connection reset, TLS error, proxy down…
                self.stats["errors"] += 1
                last_error = exc
                LOGGER.warning(
                    "%s %s failed (attempt %d/%d): %s", method, path, attempt + 1, attempts, exc
                )
            else:
                status = response.status_code
                last_status = status
                self.stats["requests"] += 1

                if status < 400:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        last_error = DivarError(f"non-JSON response from Divar: {exc}")
                        LOGGER.warning("Divar returned a non-JSON body (HTTP %s)", status)
                    else:
                        if isinstance(data, dict):
                            return data
                        last_error = DivarError("Divar returned a JSON list, expected an object")
                elif status in RETRYABLE_STATUS:
                    retry_after = _retry_after(response)
                    self.stats["errors"] += 1
                    last_error = DivarError(
                        f"HTTP {status} from Divar: {_response_excerpt(response, 120)}",
                        status=status,
                    )
                    LOGGER.warning(
                        "Divar answered HTTP %s (attempt %d/%d)%s",
                        status, attempt + 1, attempts,
                        f", waiting {retry_after:.0f}s (Retry-After)" if retry_after else "",
                    )
                else:
                    # Any other 4xx means the request itself is wrong — retrying
                    # would only burn time, so surface it to the caller.
                    raise DivarError(
                        f"Divar rejected the request: HTTP {status} "
                        f"{_response_excerpt(response)}",
                        status=status,
                        retryable=False,
                    )

            if attempt == attempts - 1:
                break
            self.stats["retries"] += 1
            self._interruptible_sleep(self._backoff_delay(attempt, retry_after))

        raise DivarError(
            f"giving up on {method} {path} after {attempts} attempts: {last_error}",
            status=last_status,
        )

    # ------------------------------------------------------------------ #
    # search bodies
    # ------------------------------------------------------------------ #
    def build_search_body(
        self, page: int = 1, cursor: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Build the JSON body Divar's own web app sends to the search endpoint."""
        settings = self.settings
        form_data: Dict[str, Any] = {}

        if self.category:
            form_data["category"] = {"str": {"value": self.category}}
        if settings.price_min is not None or settings.price_max is not None:
            number_range: Dict[str, int] = {}
            if settings.price_min is not None:
                number_range["minimum"] = int(settings.price_min)
            if settings.price_max is not None:
                number_range["maximum"] = int(settings.price_max)
            form_data["price"] = {"number_range": number_range}
        if settings.district_ids:
            form_data["districts"] = {
                "repeated_string": {"value": [str(d) for d in settings.district_ids]}
            }
        if settings.has_photo_only:
            form_data["has-photo"] = {"boolean": {}}

        search_data: Dict[str, Any] = {}
        if settings.search_query:
            search_data["query"] = settings.search_query
        if form_data:
            search_data["form_data"] = {"data": form_data}

        pagination: Dict[str, Any] = {
            "@type": PAGINATION_TYPE,
            "page": max(1, int(page)),
            "page_size": settings.page_size,
        }
        if cursor:
            pagination.update(_clean_cursor(cursor))

        body: Dict[str, Any] = {"city_ids": [settings.city_id]}
        if search_data:
            body["search_data"] = search_data
        body["pagination_data"] = pagination
        return body

    def build_legacy_body(self, page: int = 1) -> Dict[str, Any]:
        """Body for the legacy ``/v8/search/{city}/{category}`` endpoint."""
        settings = self.settings
        schema: Dict[str, Any] = {}
        if self.category:
            schema["category"] = {"value": self.category}
        if settings.search_query:
            schema["search_text"] = {"value": settings.search_query}
        now_micros = int(self._now().timestamp() * 1_000_000)
        return {"json_schema": schema, "last-post-date": now_micros, "page": max(1, page)}

    # ------------------------------------------------------------------ #
    # fetching
    # ------------------------------------------------------------------ #
    def fetch_page(self, page: int = 1, cursor: Optional[Dict[str, Any]] = None) -> SearchResult:
        """Fetch one page of listings, falling back to the legacy endpoint."""
        mode = self._endpoint or self.settings.divar_endpoint_mode
        errors: List[str] = []
        first_status: Optional[int] = None

        if mode in ("auto", "modern"):
            try:
                result = self._fetch_modern(page, cursor)
                self._endpoint = "modern"
                self.stats["modern_pages"] += 1
                return result
            except ShutdownRequested:
                raise  # a shutdown is not an endpoint problem: stop now
            except DivarError as exc:
                errors.append(f"modern: {exc}")
                first_status = exc.status
                if mode == "modern" or exc.status == 429 or not _is_fallback_status(exc):
                    raise
                LOGGER.warning("Modern endpoint unusable (%s) — trying the legacy one.", exc)

        self._endpoint = "legacy"
        try:
            result = self._fetch_legacy(page)
            self.stats["legacy_pages"] += 1
            return result
        except DivarError as exc:
            errors.append(f"legacy: {exc}")
            raise DivarError(
                " | ".join(errors), status=first_status if first_status is not None else exc.status
            ) from exc

    def _fetch_modern(self, page: int, cursor: Optional[Dict[str, Any]]) -> SearchResult:
        payload = self.request("POST", MODERN_SEARCH_PATH, self.build_search_body(page, cursor))
        widgets = payload.get("list_widgets") or []
        metadata = _posts_metadata(payload)
        posts = [
            parsed
            for parsed in (
                self._parse_row(widget.get("data") or {}, widget, metadata)
                for widget in widgets
                if widget.get("widget_type") in ("POST_ROW", "PROMOTED_POST_ROW")
            )
            if parsed is not None
        ]
        pagination = payload.get("pagination") or {}
        top_widgets = payload.get("list_top_widgets") or []
        headline = ""
        if top_widgets:
            headline = (top_widgets[0].get("data") or {}).get("text") or ""
        if not posts and not widgets:
            raise DivarError(
                "modern search returned no widgets (endpoint shape changed?)", status=None
            )
        return SearchResult(
            posts=posts,
            cursor=pagination.get("data"),
            has_next_page=bool(pagination.get("has_next_page")),
            search_id=payload.get("search_id"),
            endpoint="modern",
            page=page,
            headline=headline,
        )

    def _fetch_legacy(self, page: int) -> SearchResult:
        settings = self.settings
        path = LEGACY_SEARCH_PATH.format(
            city_id=settings.city_id, category=self.category or "all"
        )
        payload = self.request("POST", path, self.build_legacy_body(page))
        widgets = _dig(payload, "web_widgets.post_list") or payload.get("post_list")
        if widgets is None:
            raise DivarError(
                "legacy search returned no post_list (endpoint shape changed?)", status=None
            )
        posts = [
            parsed
            for parsed in (self._parse_row(item.get("data") or item, item, {}) for item in widgets)
            if parsed is not None
        ]
        cursor_value = payload.get("last_post_date")
        return SearchResult(
            posts=posts,
            cursor={"last_post_date": cursor_value} if cursor_value else None,
            has_next_page=bool(posts) and bool(cursor_value),
            endpoint="legacy",
            page=page,
        )

    def fetch_latest(self, max_posts: Optional[int] = None) -> List[DivarPost]:
        """Walk the configured pages and return de-duplicated posts, newest first.

        This is the entry point the monitoring loop uses. Network errors are
        raised as :class:`DivarError` so the caller can back off the whole cycle.
        """
        settings = self.settings
        limit = max_posts or settings.max_posts_per_cycle
        posts: List[DivarPost] = []
        seen_tokens: set = set()
        cursor: Optional[Dict[str, Any]] = None

        for page in range(1, settings.pages_per_cycle + 1):
            result = self.fetch_page(page=page, cursor=cursor)
            LOGGER.debug(
                "page %d via %s: %d posts (next=%s)",
                page, result.endpoint, len(result.posts), result.has_next_page,
            )
            for post in result.posts:
                if post.token in seen_tokens:
                    continue
                seen_tokens.add(post.token)
                posts.append(post)
                if len(posts) >= limit:
                    return self._sorted(posts)
            if not result.has_next_page or not result.cursor:
                break
            cursor = result.cursor

        return self._sorted(posts)

    @staticmethod
    def _sorted(posts: Sequence[DivarPost]) -> List[DivarPost]:
        """Newest first; posts without a timestamp keep their API order."""
        ordered = list(posts)
        ordered.sort(key=lambda p: p.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return ordered

    # ------------------------------------------------------------------ #
    # detail enrichment
    # ------------------------------------------------------------------ #
    def fetch_detail(self, token: str) -> Dict[str, Any]:
        """Fetch and parse a single post (description, exact price, category)."""
        payload = self.request("GET", DETAIL_PATH.format(token=token))
        self.stats["details"] += 1
        return self.parse_detail(payload)

    def enrich(self, posts: Sequence[DivarPost], limit: Optional[int] = None) -> int:
        """Fetch details for each post in place. Returns how many succeeded.

        Failures are logged and skipped — a missing description must never stop
        an alert for a listing whose price we already know.
        """
        enriched = 0
        for index, post in enumerate(posts):
            if limit is not None and index >= limit:
                break
            try:
                detail = self.fetch_detail(post.token)
            except DivarError as exc:
                LOGGER.warning("detail fetch failed for %s: %s", post.token, exc)
                continue
            _merge_detail(post, detail)
            enriched += 1
        return enriched

    @staticmethod
    def parse_detail(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Flatten a ``posts-v2/web/{token}`` payload into useful fields."""
        sections = {
            section.get("section_name"): section for section in payload.get("sections") or []
        }
        webengage = payload.get("webengage") or {}
        share = payload.get("share") or {}
        city = payload.get("city") or {}
        contact = payload.get("contact") or {}

        out: Dict[str, Any] = {
            "token": webengage.get("token")
            or _token_from_url(share.get("web_url") or ""),
            "url": share.get("web_url"),
            "title": share.get("title"),
            "description": None,
            "posted_text": None,
            "posted_at": None,
            "price": int(webengage["price"]) if webengage.get("price") else None,
            "credit": int(webengage["credit"]) if webengage.get("credit") else None,
            "rent": int(webengage["rent"]) if webengage.get("rent") else None,
            "category": webengage.get("category") or webengage.get("cat_3"),
            "city": city.get("name"),
            "district_slug": webengage.get("district"),
            "condition": webengage.get("status"),
            "business_type": webengage.get("business_type"),
            "brand_model": webengage.get("brand_model"),
            "image_count": webengage.get("image_count"),
            "has_chat": contact.get("chat_enabled"),
            "attributes": {},
            "images": [],
        }

        for widget in sections.get("TITLE", {}).get("widgets", []):
            data = widget.get("data") or {}
            if widget.get("widget_type") == "LEGEND_TITLE_ROW" and data.get("title"):
                out["title"] = data["title"]
            elif widget.get("widget_type") == "EXPANDABLE_SECTION":
                for sub in data.get("widget_list") or []:
                    text = (sub.get("data") or {}).get("text") or ""
                    if "انتشار آگهی" in text:
                        out["posted_text"] = text
                        out["posted_at"] = text.split("\n")[0].split("انتشار آگهی:", 1)[-1].strip()

        for widget in sections.get("DESCRIPTION", {}).get("widgets", []):
            if widget.get("widget_type") == "DESCRIPTION_ROW":
                out["description"] = (widget.get("data") or {}).get("text")

        for widget in sections.get("LIST_DATA", {}).get("widgets", []):
            data = widget.get("data") or {}
            title = data.get("title")
            value = data.get("value")
            if title and value:
                out["attributes"][str(title)] = value

        for widget in sections.get("IMAGE", {}).get("widgets", []):
            for image in (widget.get("data") or {}).get("image_list") or []:
                url = _dig(image, "url", "webp_url", "image_url")
                if url:
                    out["images"].append(url)
        if out["images"]:
            out.setdefault("image_url", out["images"][0])

        return out

    # ------------------------------------------------------------------ #
    # row normalization
    # ------------------------------------------------------------------ #
    def _parse_row(
        self,
        data: Dict[str, Any],
        widget: Dict[str, Any],
        metadata: Dict[str, Dict[str, Any]],
    ) -> Optional[DivarPost]:
        """Turn one ``POST_ROW`` widget into a :class:`DivarPost` (or ``None``)."""
        if not isinstance(data, dict):
            return None

        token = str(_dig(data, "token", "action.payload.token", "post_token") or "").strip()
        if not token:
            return None

        title = str(_dig(data, "title", "action.payload.web_info.title") or "").strip()
        price_text = str(_dig(data, "middle_description_text") or "").strip()
        bottom = str(_dig(data, "bottom_description_text") or "").strip()
        time_text, bottom_district = split_time_and_place(bottom)

        city = _dig(data, "action.payload.web_info.city_persian", "place.city", "city")
        district = _dig(
            data,
            "action.payload.web_info.district_persian",
            "place.zone",
            "place.district",
        ) or bottom_district

        post = DivarPost(
            token=token,
            title=title,
            city=str(city) if city else None,
            district=str(district) if district else None,
            time_text=time_text or bottom,
            image_url=_dig(data, "image_url", "images.main.url"),
            image_count=int(_dig(data, "image_count") or 0),
            has_chat=_dig(data, "has_chat"),
            url=f"{self.settings.divar_web_base}/v/{token}",
            raw=data,
            source_endpoint="row",
        )

        # --- price: prefer an explicit number, else parse the Persian text ---
        numeric_price = _dig(data, "price", "price_info.price", "action.payload.price")
        if isinstance(numeric_price, str) and not numeric_price.strip().isdigit():
            numeric_price = None
        if numeric_price not in (None, 0, ""):
            post.price = int(numeric_price)
            post.price_text = price_text or f"{numeric_price}"
        elif price_text:
            post.apply_price(parse_price(price_text))

        # --- publish time: exact when the API gives it, else relative text ---
        published = _dig(
            widget,
            "action_log.server_side_info.info.sort_date",
            "data.action.payload.publish_at",
            "data.time",
            "time",
            "publish_at",
            "created_at",
        )
        meta = metadata.get(token) or {}
        published = published or meta.get("sort_date")
        moment = parse_timestamp(published, self.settings.tz_offset_hours)
        if moment is not None:
            post.published_at = moment.astimezone(timezone.utc)
            post.time_is_exact = True
        else:
            age = parse_relative_time(post.time_text)
            if age is not None:
                post.published_at = self._now() - age
                post.time_is_exact = False

        # --- category ---
        extra_info = meta.get("extra_info")
        if isinstance(extra_info, str):
            try:
                extra = json.loads(extra_info)
            except ValueError:
                extra = {}
            if isinstance(extra, dict):
                post.category = extra.get("category") or post.category
        post.category = post.category or _dig(data, "category", "action.payload.category")

        return post

    # ------------------------------------------------------------------ #
    # misc
    # ------------------------------------------------------------------ #
    def probe(self) -> str:
        """Cheap connectivity/self-check. Returns a one-line summary."""
        result = self.fetch_page(page=1)
        sample = ", ".join(p.title[:24] for p in result.posts[:3]) or "(no rows)"
        return (
            f"OK via {result.endpoint}: {len(result.posts)} rows, "
            f"next={result.has_next_page}; first: {sample}"
        )

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover - closing must never raise
            pass


# --------------------------------------------------------------------------- #
# module-level helpers
# --------------------------------------------------------------------------- #
def _posts_metadata(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map token -> ``{"sort_date": ..., "extra_info": ...}`` from the page log."""
    rows = _dig(payload, "action_log.server_side_info.info.posts_metadata") or []
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("token"):
                out[str(row["token"])] = row
    return out


def _retry_after(response: requests.Response) -> Optional[float]:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:  # HTTP-date form
        from email.utils import parsedate_to_datetime

        moment = parsedate_to_datetime(raw)
        if moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None
    return None


def _response_excerpt(response: requests.Response, limit: int = 200) -> str:
    try:
        text = response.text.strip().replace("\n", " ")
    except Exception:  # pragma: no cover
        return ""
    return text[:limit]


def _is_fallback_status(error: DivarError) -> bool:
    """Should this failure make us try the other endpoint?"""
    if error.status is None:
        return True  # network/JSON problem: the other endpoint may still work
    return error.status in {400, 403, 404, 405, 410, 415}


def _token_from_url(url: str) -> str:
    if not url:
        return ""
    parts = [p for p in url.split("?")[0].rstrip("/").split("/") if p]
    return parts[-1] if parts else ""


def _merge_detail(post: DivarPost, detail: Dict[str, Any]) -> None:
    """Copy detail-page fields onto a list-row post without losing what we have."""
    if not detail:
        return
    if detail.get("description"):
        post.description = detail["description"]
    if detail.get("title") and not post.title:
        post.title = detail["title"]
    if detail.get("price"):
        # The detail payload carries the seller's structured price in Toman.
        post.price = int(detail["price"])
        post.price_text = post.price_text or str(detail["price"])
    if detail.get("category") and not post.category:
        post.category = detail["category"]
    if detail.get("city") and not post.city:
        post.city = detail["city"]
    if detail.get("condition"):
        post.condition = detail["condition"]
    if detail.get("business_type"):
        post.business_type = detail["business_type"]
    if detail.get("brand_model"):
        post.brand_model = detail["brand_model"]
    if detail.get("image_count"):
        post.image_count = int(detail["image_count"])
    if detail.get("has_chat") is not None:
        post.has_chat = detail["has_chat"]
    if detail.get("images") and not post.image_url:
        post.image_url = detail["images"][0]
    if detail.get("url") and not post.url:
        post.url = detail["url"]
    if detail.get("posted_at"):
        moment = parse_timestamp(detail["posted_at"])
        if moment:
            post.published_at = moment
            post.time_is_exact = True
    if detail.get("credit") or detail.get("rent"):
        # Rental listings: the headline price is the deposit, not a sale price.
        post.price_is_deposit = True
        post.price_note = post.price_note or "deposit_rent"


def resolve_category(value: Optional[str]) -> Optional[str]:
    """Map a friendly alias (or Persian name) onto a Divar category slug."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    return CATEGORY_ALIASES.get(text.lower(), CATEGORY_ALIASES.get(text, text))


def list_known_cities() -> List[Tuple[str, str, str]]:
    """``(slug, id, persian_name)`` triples for the built-in city map."""
    id_to_name = {value: key for key, value in CITY_IDS.items()}
    seen: set = set()
    out: List[Tuple[str, str, str]] = []
    for slug, city_id in sorted(CITY_SLUGS.items(), key=lambda kv: int(kv[1])):
        if city_id in seen:
            continue  # keep only the first (canonical) slug per id
        seen.add(city_id)
        out.append((slug, city_id, id_to_name.get(city_id, "")))
    return out


if __name__ == "__main__":  # pragma: no cover - manual helpers
    import argparse

    parser = argparse.ArgumentParser(description="Divar client utilities")
    parser.add_argument("--list-cities", action="store_true", help="print the built-in city map")
    parser.add_argument("--list-categories", action="store_true", help="print category aliases")
    parser.add_argument("--probe", action="store_true", help="fetch one page and print a summary")
    args = parser.parse_args()

    if args.list_categories:
        for alias, slug in sorted(CATEGORY_ALIASES.items()):
            print(f"{alias:28} -> {slug}")
    elif args.probe:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        client = DivarClient(Settings.load())
        try:
            print(client.probe())
        finally:
            client.close()
    else:
        for slug, city_id, name in list_known_cities():
            print(f"{city_id:>4}  {slug:20} {name}")
