"""Deal detection: resolve a trustworthy price, then apply the threshold rules.

The hard part of hunting deals on Divar is knowing *what the number means*. A
listing can show:

* the real total price — ``"۴۵٬۰۰۰٬۰۰۰ تومان"``
* a **down payment** — ``"پیش‌پرداخت ۲۰۰ میلیون"`` (the item costs much more)
* an **installment** amount — ``"قسطی ۵۰ میلیون"``
* a **deposit** for a rental — ``"ودیعه ۱۰۰ میلیون"``
* no number at all — ``"پرداخت توافقی"``, ``"رایگان"``, ``"معاوضه"``

:class:`DealFilter.resolve_price` works those cases out (including recovering the
total from the description when only a down payment is advertised), and
:meth:`DealFilter.evaluate` then applies the configured rules and explains every
accept/reject decision — the explanation is what ends up in the Telegram alert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from config import Settings
from divar_client import DivarPost
from persian import (
    extract_price_from_text,
    extract_total_price,
    find_keywords,
    format_number_fa,
    human_toman,
    is_down_payment_text,
    is_installment_text,
    parse_price,
    relative_time_fa,
)

__all__ = ["DealFilter", "PriceDecision", "Verdict", "CycleStats"]

LOGGER = logging.getLogger("divar.filter")

PRICE_NOTE_FA = {
    "negotiable": "توافقی",
    "free": "رایگان",
    "exchange": "معاوضه",
    "deposit_rent": "ودیعه / رهن",
    "from_price": "قیمت پایه (از)",
}


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
@dataclass
class PriceDecision:
    """The price we will actually judge a listing by."""

    value: Optional[int] = None
    source: str = "unknown"  # api_number | api_text | description | total_recovered
    note: Optional[str] = None
    is_down_payment: bool = False
    is_installment: bool = False
    is_deposit: bool = False
    warnings: List[str] = field(default_factory=list)
    raw_text: str = ""

    @property
    def usable(self) -> bool:
        return self.value is not None

    @property
    def display(self) -> str:
        """Persian price line for the alert, e.g. ``"۴۵۰ میلیون تومان"``."""
        if self.value is not None:
            text = human_toman(self.value)
            if self.source == "total_recovered":
                return f"{text} (قیمت کل، از متن آگهی)"
            if self.source == "description":
                return f"{text} (از توضیحات)"
            return text
        return PRICE_NOTE_FA.get(self.note or "", "بدون قیمت")

    def detail_line(self) -> str:
        """Price plus any caveat worth showing to the reader."""
        parts = [self.display]
        if self.raw_text and self.raw_text not in parts[0]:
            parts.append(f"متن اصلی: {self.raw_text}")
        return " — ".join(parts)


@dataclass
class Verdict:
    """The outcome of judging one listing."""

    post: DivarPost
    is_deal: bool
    price: PriceDecision
    reasons: List[str] = field(default_factory=list)
    rejections: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    score: int = 0

    @property
    def price_value(self) -> Optional[int]:
        return self.price.value

    def summary_fa(self) -> str:
        """Short Persian explanation, used in the alert body."""
        lines = self.reasons + [f"⚠️ {w}" for w in self.warnings]
        return "\n".join(lines) if lines else "—"

    def explain(self) -> str:
        """One-line explanation for logs (accepts *and* rejects)."""
        if self.is_deal:
            return "DEAL: " + "; ".join(self.reasons + [f"warn: {w}" for w in self.warnings])
        return "skip: " + "; ".join(self.rejections)

    def to_dict(self) -> Dict[str, object]:
        return {
            "token": self.post.token,
            "is_deal": self.is_deal,
            "price": self.price.value,
            "price_source": self.price.source,
            "reasons": self.reasons,
            "rejections": self.rejections,
            "warnings": self.warnings,
            "score": self.score,
        }


@dataclass
class CycleStats:
    """Aggregate counters for one monitoring cycle."""

    fetched: int = 0
    fresh: int = 0
    deals: int = 0
    rejected: int = 0
    rejection_reasons: Dict[str, int] = field(default_factory=dict)

    def note_rejection(self, reason: str) -> None:
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1

    def summary(self) -> str:
        top = ", ".join(f"{k}={v}" for k, v in sorted(self.rejection_reasons.items(), key=lambda kv: -kv[1])[:4])
        return (
            f"fetched={self.fetched} fresh={self.fresh} deals={self.deals} "
            f"rejected={self.rejected}" + (f" [{top}]" if top else "")
        )


# --------------------------------------------------------------------------- #
# filter
# --------------------------------------------------------------------------- #
class DealFilter:
    """Applies the configured deal rules to normalized Divar posts."""

    def __init__(self, settings: Settings, now: Optional[Callable[[], datetime]] = None) -> None:
        self.settings = settings
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ #
    def resolve_price(self, post: DivarPost) -> PriceDecision:
        """Work out the Toman amount this listing should be judged by.

        The structured price (``post.price``) says *how much*; the Persian price
        text and the description say *what kind* of amount it is. Both are read,
        because a listing can carry a clean number and still be a down payment.
        """
        decision = PriceDecision(raw_text=post.price_text)
        decision.note = post.price_note
        decision.is_down_payment = post.price_is_down_payment
        decision.is_installment = post.price_is_installment
        decision.is_deposit = post.price_is_deposit

        # Re-read the Persian price text: it may carry qualifiers the structured
        # field does not (پیش‌پرداخت / قسطی / ودیعه / توافقی …).
        parsed = parse_price(post.price_text) if post.price_text else None
        if parsed is not None:
            decision.is_down_payment = decision.is_down_payment or parsed.is_down_payment
            decision.is_installment = decision.is_installment or parsed.is_installment
            decision.is_deposit = decision.is_deposit or parsed.is_deposit
            if decision.note is None:
                decision.note = parsed.note

        # The title/description can reveal an installment plan too.
        if is_down_payment_text(post.title, post.description):
            decision.is_down_payment = True
        if is_installment_text(post.title, post.description):
            decision.is_installment = True

        text_blob = " ".join(
            part for part in (post.price_text, post.title, post.description or "") if part
        )

        if post.price is not None:
            decision.value = int(post.price)
            decision.source = "api_number" if not parsed or parsed.value is None else "api_text"
        elif parsed is not None and parsed.value is not None:
            decision.value = parsed.value
            decision.source = "api_text"
        else:
            # No number anywhere structured: try the description
            # ("قیمت: ۴۵ میلیون تومان") — but never a bare model number.
            from_text = extract_price_from_text(post.description or post.title)
            if from_text:
                decision.value = from_text
                decision.source = "description"
                decision.warnings.append("قیمت از توضیحات آگهی استخراج شد")

        # A down payment / installment / deposit is *not* the total price.
        if decision.is_down_payment or decision.is_installment or decision.is_deposit:
            recovered = extract_total_price(text_blob, down_payment=decision.value)
            if recovered and (decision.value is None or recovered > decision.value):
                decision.value = recovered
                decision.source = "total_recovered"
                decision.is_down_payment = False
                decision.is_installment = False
                decision.is_deposit = False
                decision.warnings.append(
                    "قیمت نمایشی پیش‌پرداخت/قسط بود؛ قیمت کل از متن آگهی استخراج شد"
                )

        if decision.value is not None and decision.value <= 0:
            decision.value = None
            decision.note = decision.note or "negotiable"
        return decision

    # ------------------------------------------------------------------ #
    def evaluate(self, post: DivarPost, stats: Optional[CycleStats] = None) -> Verdict:
        """Judge one post. ``Verdict.is_deal`` says whether to alert on it."""
        settings = self.settings
        if stats is not None:
            stats.fetched += 1
        price = self.resolve_price(post)
        verdict = Verdict(post=post, is_deal=False, price=price, warnings=list(price.warnings))

        def reject(reason: str, label: str) -> Verdict:
            verdict.rejections.append(reason)
            if stats is not None:
                stats.note_rejection(label)
                stats.rejected += 1
            return verdict

        if not post.token:
            return reject("no token in payload", "no_token")

        # 1) freshness — avoids replaying stale listings after downtime
        age_seconds = post.age_seconds(self._now())
        if settings.max_age_minutes and age_seconds is not None:
            limit = settings.max_age_minutes * 60
            if age_seconds > limit:
                return reject(
                    f"older than {settings.max_age_minutes} min ({age_seconds / 3600:.1f} h)",
                    "too_old",
                )
        if age_seconds is None:
            verdict.warnings.append("زمان انتشار نامشخص است")

        # 2) we need a price to compare against a threshold
        if price.value is None:
            if settings.require_price:
                label = price.note or "no_price"
                return reject(
                    f"no usable price ({PRICE_NOTE_FA.get(price.note or '', price.note) or 'نامشخص'})",
                    f"no_price:{label}",
                )
            verdict.warnings.append("آگهی بدون قیمت اعلامی")

        # 3) down payment / installments / deposit-only listings
        if (
            price.is_down_payment
            or price.is_installment
            or price.is_deposit
        ):
            if settings.skip_down_payment_ads:
                if price.is_down_payment:
                    kind_en, kind_fa = "down payment", "پیش‌پرداخت"
                elif price.is_installment:
                    kind_en, kind_fa = "installment", "اقساطی"
                else:
                    kind_en, kind_fa = "deposit", "ودیعه/رهن"
                return reject(
                    f"advertised price is a {kind_en} ({kind_fa}), not the total",
                    f"not_total_price:{kind_en}",
                )
            verdict.warnings.append("قیمت اعلامی ممکن است پیش‌پرداخت/قسط باشد، نه قیمت کل")

        # 4) absurdly cheap listings are almost always scams or wrong categories
        if price.value is not None and settings.deal_price_floor is not None:
            if price.value < settings.deal_price_floor:
                return reject(
                    f"below floor ({format_number_fa(price.value)} < "
                    f"{format_number_fa(settings.deal_price_floor)})",
                    "below_floor",
                )

        # 5) the main threshold
        if price.value is not None and settings.deal_max_price is not None:
            if price.value > settings.deal_max_price:
                return reject(
                    f"above threshold ({format_number_fa(price.value)} > "
                    f"{format_number_fa(settings.deal_max_price)})",
                    "above_threshold",
                )
            discount = 1 - (price.value / settings.deal_max_price)
            verdict.score = max(1, min(99, round(discount * 100)))
            verdict.reasons.append(
                f"💰 {human_toman(price.value)} — {format_number_fa(verdict.score)}٪ زیر سقف "
                f"{human_toman(settings.deal_max_price)}"
            )
        elif price.value is not None:
            verdict.score = 50
            verdict.reasons.append(f"💰 {human_toman(price.value)}")

        # 6) keyword rules over title + description
        haystack = post.searchable_text()
        if settings.exclude_keywords:
            hits = find_keywords(haystack, settings.exclude_keywords)
            if hits:
                return reject(f"excluded keyword(s): {', '.join(hits)}", "excluded_keyword")
        if settings.include_keywords:
            hits = find_keywords(haystack, settings.include_keywords)
            if not hits:
                return reject("no required keyword in title/description", "missing_keyword")
            verdict.reasons.append(f"🔎 کلیدواژه: {'، '.join(hits)}")

        # 7) seller / media requirements
        if settings.only_personal_ads and post.business_type and post.business_type != "personal":
            return reject(f"not a private seller (business_type={post.business_type})", "business_seller")
        if settings.has_photo_only and post.image_count == 0 and not post.image_url:
            return reject("no photo", "no_photo")

        # 8) freshness is a selling point — say so
        if age_seconds is not None and age_seconds <= 30 * 60:
            verdict.reasons.append(f"🕒 آگهی تازه: {relative_time_fa(post.published_at, self._now())}")
        if post.has_chat:
            verdict.reasons.append("💬 چت دیوار فعال است")

        verdict.is_deal = True
        if stats is not None:
            stats.deals += 1
        return verdict

    # ------------------------------------------------------------------ #
    def filter_posts(
        self, posts: Iterable[DivarPost]
    ) -> Tuple[List[Verdict], CycleStats]:
        """Judge a batch, returning accepted verdicts (best deals first) + stats."""
        stats = CycleStats()
        accepted: List[Verdict] = []
        for post in posts:
            verdict = self.evaluate(post, stats)  # evaluate() keeps the counters
            if verdict.is_deal:
                accepted.append(verdict)
            else:
                LOGGER.debug("%s -> %s", post.token, verdict.explain())
        accepted.sort(key=_sort_key, reverse=True)
        return accepted, stats


def _sort_key(verdict: Verdict) -> Tuple[float, float]:
    """Rank deals: biggest discount first, newest first on a tie."""
    age = verdict.post.age_seconds() or 0.0
    return (float(verdict.score), -age)
