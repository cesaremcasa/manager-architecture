from collections.abc import Mapping
from typing import Any

import httpx


class ResendAPIError(RuntimeError):
    pass


class ResendReceivingClient:
    """Small async adapter for the Resend Receiving API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.resend.com",
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("RESEND_API_KEY is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    async def get_received_email(self, email_id: str) -> Mapping[str, Any]:
        response = await self._request("GET", f"/emails/receiving/{email_id}")
        return self._object_response(response)

    async def list_attachments(self, email_id: str) -> list[Mapping[str, Any]]:
        response = await self._request("GET", f"/emails/receiving/{email_id}/attachments")
        data = response.get("data", [])
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise ResendAPIError("Resend attachment response has an invalid shape")
        return data

    async def download(self, download_url: str) -> bytes:
        async with self._client(authenticated=False) as client:
            response = await client.get(download_url)
        if response.is_error:
            raise ResendAPIError(f"Resend download failed with HTTP {response.status_code}")
        return response.content

    async def _request(self, method: str, path: str) -> Mapping[str, Any]:
        async with self._client(authenticated=True) as client:
            response = await client.request(method, f"{self._base_url}{path}")
        if response.is_error:
            raise ResendAPIError(f"Resend API failed with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise ResendAPIError("Resend API returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise ResendAPIError("Resend API returned an invalid response")
        return payload

    @staticmethod
    def _object_response(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not payload.get("id"):
            raise ResendAPIError("Resend email response has no id")
        return payload

    def _client(self, *, authenticated: bool) -> httpx.AsyncClient:
        headers = {"User-Agent": "manager-api/0.1"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return httpx.AsyncClient(headers=headers, timeout=self._timeout, transport=self._transport)
