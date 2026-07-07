"""Gift-card fulfillment behind an interface so tests (and local dev without
credentials) run against a stub. Real orders go to Tremendous — sandbox
(testflight) by default; point tremendous_base_url at production to go live.
"""

import logging
import uuid
from decimal import Decimal
from typing import Protocol

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class FulfillmentError(Exception):
    pass


class FulfillmentClient(Protocol):
    def create_order(self, *, email: str, usd: Decimal, external_id: str) -> str:
        """Deliver `usd` to `email`; returns the provider order id.
        `external_id` is the redemption id — providers dedupe on it, so a
        retried approval can never pay twice."""
        ...


class TremendousClient:
    def __init__(self, settings: Settings):
        self._settings = settings

    def create_order(self, *, email: str, usd: Decimal, external_id: str) -> str:
        s = self._settings
        reward: dict = {
            "value": {"denomination": float(usd), "currency_code": "USD"},
            "delivery": {"method": "EMAIL"},
            "recipient": {"email": email},
        }
        if s.tremendous_campaign_id:
            reward["campaign_id"] = s.tremendous_campaign_id
        body = {
            "external_id": external_id,
            "payment": {"funding_source_id": s.tremendous_funding_source_id},
            "rewards": [reward],
        }
        try:
            resp = httpx.post(
                f"{s.tremendous_base_url}/orders",
                json=body,
                headers={"Authorization": f"Bearer {s.tremendous_api_key}"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise FulfillmentError(f"tremendous request failed: {exc}") from exc
        if resp.status_code == 201:
            return resp.json()["order"]["id"]
        # 409 duplicate external_id = an earlier attempt succeeded; fetch it.
        if resp.status_code == 409:
            return self._find_by_external_id(external_id)
        raise FulfillmentError(
            f"tremendous order failed: {resp.status_code} {resp.text[:500]}"
        )

    def _find_by_external_id(self, external_id: str) -> str:
        s = self._settings
        resp = httpx.get(
            f"{s.tremendous_base_url}/orders/{external_id}",
            headers={"Authorization": f"Bearer {s.tremendous_api_key}"},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()["order"]["id"]
        raise FulfillmentError(
            f"tremendous duplicate lookup failed: {resp.status_code} {resp.text[:500]}"
        )


class StubFulfillment:
    """Records orders in memory. Used in tests and whenever no Tremendous
    API key is configured."""

    def __init__(self):
        self.orders: list[dict] = []
        self.fail = False  # tests flip this to exercise the failure path

    def create_order(self, *, email: str, usd: Decimal, external_id: str) -> str:
        if self.fail:
            raise FulfillmentError("stub configured to fail")
        for order in self.orders:  # same external_id -> same order id
            if order["external_id"] == external_id:
                return order["id"]
        order_id = f"stub-{uuid.uuid4()}"
        self.orders.append(
            {"id": order_id, "email": email, "usd": usd, "external_id": external_id}
        )
        return order_id


def build_fulfillment_client() -> FulfillmentClient:
    settings = get_settings()
    if settings.tremendous_api_key:
        return TremendousClient(settings)
    logger.warning("TREMENDOUS_API_KEY not set; using in-memory stub fulfillment")
    return StubFulfillment()
