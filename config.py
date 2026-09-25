"""Configuration loading and validation for the Divar deal hunter.

All settings come from environment variables (optionally via a ``.env`` file)
and are exposed as a single immutable :class:`Settings` object, so the rest of
the codebase never touches ``os.environ`` directly and nothing sensitive is
hard-coded next to business logic.

Usage::

    from config import Settings
    settings = Settings.load()          # reads .env + process environment
    print(settings.telegram_chat_id)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from persian import format_number_fa

try:  # pragma: no cover - trivial import guard
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is optional at import time
    load_dotenv = None  # type: ignore[assignment]

__all__ = ["Settings", "ConfigError", "TRUE_VALUES", "FALSE_VALUES"]

TRUE_VALUES = {"1", "true", "yes", "y", "on", "ha", "بله"}
FALSE_VALUES = {"0", "false", "no", "n", "off", "na", "خیر", ""}


class ConfigError(RuntimeError):
    """Raised when the configuration is incomplete or inconsistent."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        detail = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(
            "Invalid configuration:\n"
            f"{detail}\n"
            "Copy .env.example to .env and fill in the missing values."
        )


# --------------------------------------------------------------------------- #
# small typed environment helpers
# --------------------------------------------------------------------------- #
def _raw(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value != "" else default


def _str(name: str, default: str = "") -> str:
    return _raw(name, default) or default


def _opt_str(name: str) -> Optional[str]:
    return _raw(name, None)


def _bool(name: str, default: bool = False) -> bool:
    value = _raw(name)
    if value is None:
        return default
    low = value.lower()
    if low in TRUE_VALUES:
        return True
    if low in FALSE_VALUES:
        return False
    raise ConfigError([f"{name}: expected a boolean (true/false), got {value!r}"])


def _int(name: str, default: Optional[int] = None) -> Optional[int]:
    value = _raw(name)
    if value is None:
        return default
    cleaned = value.replace(",", "").replace("_", "").replace(" ", "")
    try:
        return int(cleaned)
    except ValueError:
        raise ConfigError([f"{name}: expected an integer, got {value!r}"]) from None


def _float(name: str, default: float) -> float:
    value = _raw(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        raise ConfigError([f"{name}: expected a number, got {value!r}"]) from None


def _list(name: str) -> List[str]:
    value = _raw(name)
    if not value:
        return []
    parts = value.replace("،", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    """Everything the service needs to run, validated once at startup."""

    # --- Telegram ------------------------------------------------------- #
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_proxy: Optional[str] = None
    telegram_api_base: str = "https://api.telegram.org"
    telegram_timeout: float = 15.0
    telegram_max_retries: int = 4
    alert_spacing_seconds: float = 1.5
    max_alerts_per_cycle: int = 20
    max_alert_attempts: int = 3

    # --- Divar target --------------------------------------------------- #
    city: str = "tehran"
    city_id: str = "1"
    category: Optional[str] = None
    search_query: Optional[str] = None
    district_ids: List[str] = None  # type: ignore[assignment]
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    has_photo_only: bool = False
    only_personal_ads: bool = False
    pages_per_cycle: int = 1
    page_size: int = 24
    max_posts_per_cycle: int = 60
    fetch_details: bool = False
    max_detail_lookups: int = 10
    max_age_minutes: int = 180

    # --- Deal rules ----------------------------------------------------- #
    deal_max_price: Optional[int] = 50_000_000
    deal_price_floor: Optional[int] = 1_000_000
    require_price: bool = True
    skip_down_payment_ads: bool = True
    include_keywords: List[str] = None  # type: ignore[assignment]
    exclude_keywords: List[str] = None  # type: ignore[assignment]

    # --- Network hygiene ------------------------------------------------ #
    check_interval_seconds: float = 60.0
    request_delay_min: float = 4.0
    request_delay_max: float = 9.0
    request_timeout: float = 20.0
    max_retries: int = 5
    backoff_base: float = 1.5
    backoff_max: float = 60.0
    divar_proxy: Optional[str] = None
    divar_api_base: str = "https://api.divar.ir"
    divar_web_base: str = "https://divar.ir"
    divar_endpoint_mode: str = "auto"

    # --- State & runtime ------------------------------------------------ #
    db_path: str = "data/seen_tokens.db"
    prune_days: int = 14
    bootstrap_silent: bool = True
    log_level: str = "INFO"
    log_file: Optional[str] = None
    dry_run: bool = False
    tz_offset_hours: float = 3.5

    def __post_init__(self) -> None:
        # Mutable defaults: keep the dataclass frozen but never share lists.
        for name in ("district_ids", "include_keywords", "exclude_keywords"):
            if getattr(self, name) is None:
                object.__setattr__(self, name, [])

    # ------------------------------------------------------------------ #
    @classmethod
    def load(
        cls,
        env_file: Optional[Union[os.PathLike, str]] = ".env",
        overrides: Optional[Dict[str, Any]] = None,
        validate: bool = True,
    ) -> "Settings":
        """Build settings from ``.env`` + process env, then validate.

        ``overrides`` wins over everything and is handy for CLI flags and tests.
        """
        if env_file is not None and load_dotenv is not None:
            path = Path(env_file)
            if path.exists():
                # Do not let the file clobber explicitly exported variables.
                load_dotenv(path, override=False)

        shared_proxy = _opt_str("PROXY_URL") or _opt_str("HTTPS_PROXY") or _opt_str("ALL_PROXY")
        city_raw = _str("CITY", "tehran")
        city_id_raw = _opt_str("CITY_ID")
        city_id, city_problems = resolve_city_id(city_raw, city_id_raw)

        values: Dict[str, Any] = dict(
            telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
            telegram_proxy=_opt_str("TELEGRAM_PROXY") or shared_proxy,
            telegram_api_base=_str("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
            telegram_timeout=_float("TELEGRAM_TIMEOUT", 15.0),
            telegram_max_retries=max(1, _int("TELEGRAM_MAX_RETRIES", 4) or 4),
            alert_spacing_seconds=max(0.0, _float("ALERT_SPACING_SECONDS", 1.5)),
            max_alerts_per_cycle=max(1, _int("MAX_ALERTS_PER_CYCLE", 20) or 20),
            max_alert_attempts=max(1, _int("MAX_ALERT_ATTEMPTS", 3) or 3),
            city=city_raw,
            city_id=city_id,
            category=_opt_str("CATEGORY"),
            search_query=_opt_str("SEARCH_QUERY"),
            district_ids=[str(d) for d in _list("DISTRICT_IDS")],
            price_min=_int("PRICE_MIN"),
            price_max=_int("PRICE_MAX"),
            has_photo_only=_bool("HAS_PHOTO_ONLY", False),
            only_personal_ads=_bool("ONLY_PERSONAL_ADS", False),
            pages_per_cycle=max(1, _int("PAGES_PER_CYCLE", 1) or 1),
            page_size=min(60, max(1, _int("PAGE_SIZE", 24) or 24)),
            max_posts_per_cycle=max(1, _int("MAX_POSTS_PER_CYCLE", 60) or 60),
            fetch_details=_bool("FETCH_DETAILS", False),
            max_detail_lookups=max(1, _int("MAX_DETAIL_LOOKUPS", 10) or 10),
            max_age_minutes=max(0, _int("MAX_AGE_MINUTES", 180) or 0),
            deal_max_price=_int("DEAL_MAX_PRICE", 50_000_000),
            deal_price_floor=_int("DEAL_PRICE_FLOOR", 1_000_000),
            require_price=_bool("REQUIRE_PRICE", True),
            skip_down_payment_ads=_bool("SKIP_DOWN_PAYMENT_ADS", True),
            include_keywords=_list("INCLUDE_KEYWORDS"),
            exclude_keywords=_list("EXCLUDE_KEYWORDS"),
            check_interval_seconds=max(5.0, _float("CHECK_INTERVAL_SECONDS", 60.0)),
            request_delay_min=max(0.0, _float("REQUEST_DELAY_MIN", 4.0)),
            request_delay_max=max(0.0, _float("REQUEST_DELAY_MAX", 9.0)),
            request_timeout=max(1.0, _float("REQUEST_TIMEOUT", 20.0)),
            max_retries=max(1, _int("MAX_RETRIES", 5) or 5),
            backoff_base=max(0.1, _float("BACKOFF_BASE", 1.5)),
            backoff_max=max(0.1, _float("BACKOFF_MAX", 60.0)),
            divar_proxy=_opt_str("DIVAR_PROXY") or shared_proxy,
            divar_api_base=_str("DIVAR_API_BASE", "https://api.divar.ir").rstrip("/"),
            divar_web_base=_str("DIVAR_WEB_BASE", "https://divar.ir").rstrip("/"),
            divar_endpoint_mode=_str("DIVAR_ENDPOINT_MODE", "auto").lower(),
            db_path=_str("DB_PATH", "data/seen_tokens.db"),
            prune_days=max(0, _int("PRUNE_DAYS", 14) or 0),
            bootstrap_silent=_bool("BOOTSTRAP_SILENT", True),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
            log_file=_opt_str("LOG_FILE"),
            dry_run=_bool("DRY_RUN", False),
            tz_offset_hours=_float("TZ_OFFSET_HOURS", 3.5),
        )

        if overrides:
            for key, value in overrides.items():
                if value is None:
                    continue
                if key not in values:
                    raise ConfigError([f"unknown setting {key!r}"])
                values[key] = value

        settings = cls(**values)
        problems = settings.problems() + city_problems
        if validate and problems:
            raise ConfigError(problems)
        return settings

    # ------------------------------------------------------------------ #
    def problems(self) -> List[str]:
        """Return a list of human-readable configuration problems."""
        problems: List[str] = []

        if not self.dry_run:
            if not self.telegram_bot_token:
                problems.append("TELEGRAM_BOT_TOKEN is empty (get one from @BotFather)")
            if not self.telegram_chat_id:
                problems.append("TELEGRAM_CHAT_ID is empty (numeric id or @channel handle)")

        if self.request_delay_min > self.request_delay_max:
            problems.append("REQUEST_DELAY_MIN must be <= REQUEST_DELAY_MAX")

        if (
            self.price_min is not None
            and self.deal_max_price is not None
            and self.price_min > self.deal_max_price
        ):
            problems.append("PRICE_MIN is above DEAL_MAX_PRICE — nothing can ever match")

        if (
            self.price_max is not None
            and self.deal_price_floor is not None
            and self.price_max < self.deal_price_floor
        ):
            problems.append("PRICE_MAX is below DEAL_PRICE_FLOOR — nothing can ever match")

        if self.divar_endpoint_mode not in {"auto", "modern", "legacy"}:
            problems.append("DIVAR_ENDPOINT_MODE must be one of: auto, modern, legacy")

        if self.include_keywords and self.exclude_keywords:
            overlap = {k.lower() for k in self.include_keywords} & {
                k.lower() for k in self.exclude_keywords
            }
            if overlap:
                problems.append(
                    "INCLUDE_KEYWORDS and EXCLUDE_KEYWORDS overlap: "
                    + ", ".join(sorted(overlap))
                )

        if self.telegram_proxy and not _proxy_supported(self.telegram_proxy):
            problems.append(f"TELEGRAM_PROXY {self.telegram_proxy!r}: unsupported scheme")
        if self.divar_proxy and not _proxy_supported(self.divar_proxy):
            problems.append(f"DIVAR_PROXY {self.divar_proxy!r}: unsupported scheme")

        return problems

    # ------------------------------------------------------------------ #
    def with_updates(self, **changes: Any) -> "Settings":
        """Return a copy with selected fields replaced."""
        unknown = [k for k in changes if k not in {f.name for f in fields(self)}]
        if unknown:
            raise ConfigError([f"unknown setting {k!r}" for k in unknown])
        return replace(self, **changes)

    def proxies_for(self, target: str) -> Optional[Dict[str, str]]:
        """requests-style proxies dict for ``divar`` or ``telegram``."""
        url = self.divar_proxy if target == "divar" else self.telegram_proxy
        if not url:
            return None
        return {"http": url, "https": url}

    def masked(self) -> Dict[str, Any]:
        """Settings as a dict with secrets redacted — safe to log."""
        out: Dict[str, Any] = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if "token" in field.name or "proxy" in field.name:
                out[field.name] = _mask(str(value)) if value else value
            else:
                out[field.name] = value
        return out

    def describe(self) -> str:
        """A short, human-readable summary of the active hunt configuration."""
        ceiling = (
            format_number_fa(self.deal_max_price) + " تومان"
            if self.deal_max_price is not None
            else "off"
        )
        parts = [
            f"city={self.city} (id={self.city_id})",
            f"category={self.category or 'ALL'}",
            f"query={self.search_query or '-'}",
            f"deal_max_price={ceiling}",
            f"interval={self.check_interval_seconds:.0f}s",
            f"dry_run={self.dry_run}",
        ]
        return " | ".join(parts)


# --------------------------------------------------------------------------- #
def _proxy_supported(url: str) -> bool:
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    return scheme in {"http", "https", "socks5", "socks5h", "socks4", "socks4a"}


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def resolve_city_id(city: str, explicit_id: Optional[str] = None) -> tuple:
    """Resolve a city given as id, ASCII slug or Persian name.

    Returns ``(city_id, problems)`` so the caller can report a helpful error
    together with the rest of the configuration problems.
    """
    # Imported lazily: divar_client imports config, so importing it at module
    # scope here would create an import cycle.
    from divar_client import CITY_IDS, CITY_SLUGS

    problems: List[str] = []
    if explicit_id:
        if explicit_id.isdigit():
            return explicit_id, problems
        problems.append(f"CITY_ID must be numeric, got {explicit_id!r}")

    if not city:
        return "1", problems
    if city.isdigit():
        return city, problems

    key = city.strip().lower()
    if key in CITY_SLUGS:
        return CITY_SLUGS[key], problems
    if city.strip() in CITY_IDS:
        return CITY_IDS[city.strip()], problems

    # Unknown city: keep it as a slug and let Divar answer; warn loudly.
    problems.append(
        f"CITY {city!r} is not in the built-in city map. Pass the numeric "
        "CITY_ID instead (Divar city ids appear in divar.ir URLs / the API)."
    )
    return key, problems


def load_or_die(env_file: Optional[str] = ".env", overrides: Optional[Dict[str, Any]] = None) -> Settings:
    """``Settings.load`` but exits with a readable message instead of a traceback."""
    import sys

    try:
        return Settings.load(env_file=env_file, overrides=overrides)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":  # pragma: no cover - convenience: python config.py
    settings = load_or_die()
    for key, value in settings.masked().items():
        print(f"{key}={value}")
