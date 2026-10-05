"""Yelp Places adapter; persistence and tenant ownership stay outside the client."""

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import httpx

from app.domain.market import CompetitorSearch, MarketBusiness, MarketReviewExcerpt, MarketSource


class YelpAPIError(RuntimeError):
    pass


class YelpPlacesClient:
    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None, base_url: str = "https://api.yelp.com/v3") -> None:
        if not api_key.strip():
            raise ValueError("YELP_API_KEY is required")
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def search(self, request: CompetitorSearch, *, limit: int = 20) -> tuple[MarketBusiness, ...]:
        response = self._client.get(
            "/businesses/search",
            params={
                "latitude": str(request.latitude),
                "longitude": str(request.longitude),
                "radius": request.radius_meters,
                "term": request.term,
                "limit": max(1, min(limit, 50)),
            },
        )
        payload = self._json(response)
        return tuple(_business(item) for item in payload.get("businesses", []))

    def business(self, provider_id: str) -> MarketBusiness:
        response = self._client.get(f"/businesses/{provider_id}")
        return _business(self._json(response))

    def reviews(self, provider_id: str) -> tuple[MarketReviewExcerpt, ...]:
        response = self._client.get(f"/businesses/{provider_id}/reviews")
        payload = self._json(response)
        return tuple(
            MarketReviewExcerpt(
                provider_review_id=str(item.get("id") or f"{provider_id}:{index}"),
                business_provider_id=provider_id,
                text=str(item.get("text", "")),
                rating=int(item["rating"]) if item.get("rating") is not None else None,
                url=str(item["url"]) if item.get("url") else None,
            )
            for index, item in enumerate(payload.get("reviews", []), start=1)
        )

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        if response.is_error:
            raise YelpAPIError(f"Yelp request failed with status {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise YelpAPIError("Yelp returned an invalid response")
        return payload


class MockYelpPlacesClient:
    """Offline client used by the first-store demo and frontend development."""

    def __init__(self, businesses: Iterable[MarketBusiness], reviews: Iterable[MarketReviewExcerpt] = ()) -> None:
        self._businesses = tuple(businesses)
        self._reviews = tuple(reviews)

    def search(self, request: CompetitorSearch, *, limit: int = 20) -> tuple[MarketBusiness, ...]:
        del request
        return self._businesses[: max(1, min(limit, 50))]

    def business(self, provider_id: str) -> MarketBusiness:
        for business in self._businesses:
            if business.provider_id == provider_id:
                return business
        raise YelpAPIError("mock Yelp business not found")

    def reviews(self, provider_id: str) -> tuple[MarketReviewExcerpt, ...]:
        return tuple(review for review in self._reviews if review.business_provider_id == provider_id)


def _business(item: dict[str, Any]) -> MarketBusiness:
    location = item.get("location") or {}
    coordinates = item.get("coordinates") or {}
    return MarketBusiness(
        provider_id=str(item.get("id") or item.get("alias") or ""),
        name=str(item.get("name") or ""),
        source=MarketSource.YELP,
        url=str(item["url"]) if item.get("url") else None,
        rating=Decimal(str(item["rating"])) if item.get("rating") is not None else None,
        review_count=int(item["review_count"]) if item.get("review_count") is not None else None,
        price=str(item["price"]) if item.get("price") else None,
        categories=tuple(str(category.get("title", "")) for category in item.get("categories", [])),
        address=str(location["address1"]) if location.get("address1") else None,
        city=str(location["city"]) if location.get("city") else None,
        state=str(location["state"]) if location.get("state") else None,
        latitude=Decimal(str(coordinates["latitude"])) if coordinates.get("latitude") is not None else None,
        longitude=Decimal(str(coordinates["longitude"])) if coordinates.get("longitude") is not None else None,
    )


__all__ = ["MockYelpPlacesClient", "YelpAPIError", "YelpPlacesClient"]
