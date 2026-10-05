"""Provider-independent market observations for the restaurant and competitors."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class MarketSource(StrEnum):
    YELP = "yelp"


@dataclass(frozen=True)
class MarketBusiness:
    provider_id: str
    name: str
    source: MarketSource
    url: str | None = None
    rating: Decimal | None = None
    review_count: int | None = None
    price: str | None = None
    categories: tuple[str, ...] = ()
    address: str | None = None
    city: str | None = None
    state: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.name.strip():
            raise ValueError("provider_id and name are required")
        if self.rating is not None and not Decimal("0") <= self.rating <= Decimal("5"):
            raise ValueError("rating must be between 0 and 5")
        if self.review_count is not None and self.review_count < 0:
            raise ValueError("review_count must not be negative")


@dataclass(frozen=True)
class MarketReviewExcerpt:
    provider_review_id: str
    business_provider_id: str
    text: str
    rating: int | None = None
    url: str | None = None


@dataclass(frozen=True)
class MarketObservation:
    business: MarketBusiness
    observed_at: datetime
    is_owned_business: bool
    review_excerpts: tuple[MarketReviewExcerpt, ...] = ()

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True)
class CompetitorSearch:
    latitude: Decimal
    longitude: Decimal
    radius_meters: int = 5000
    term: str = "restaurants"

    def __post_init__(self) -> None:
        if not -90 <= self.latitude <= 90 or not -180 <= self.longitude <= 180:
            raise ValueError("latitude/longitude are invalid")
        if not 1 <= self.radius_meters <= 40_000:
            raise ValueError("radius_meters must be between 1 and 40000")
        if not self.term.strip():
            raise ValueError("term is required")


__all__ = [
    "CompetitorSearch",
    "MarketBusiness",
    "MarketObservation",
    "MarketReviewExcerpt",
    "MarketSource",
]
