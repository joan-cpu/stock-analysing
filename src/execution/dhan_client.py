"""Minimal Dhan v2 REST client.

A thin wrapper around the two endpoints this project needs:

* ``GET  /fundlimit`` - account funds (used by the risk layer)
* ``POST /orders``    - place an order (used by the executor)

The base URL (sandbox vs live) and auth headers come from ``Settings`` so there
is exactly one place that decides which environment is targeted.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class DhanAPIError(RuntimeError):
    """Raised when a Dhan REST call fails."""


class DhanClient:
    """Small HTTP client for the Dhan v2 API."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    @property
    def base_url(self) -> str:
        return self.settings.base_url.rstrip("/")

    def _require_creds(self) -> None:
        if not self.settings.has_dhan:
            raise DhanAPIError(
                "Dhan credentials missing. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env."
            )

    def get_fund_limit(self) -> Dict[str, Any]:
        """GET /fundlimit -> account funds."""
        self._require_creds()
        import requests

        resp = requests.get(
            f"{self.base_url}/fundlimit",
            headers=self.settings.auth_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def place_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /orders -> order acknowledgement ({orderId, orderStatus})."""
        self._require_creds()
        import requests

        resp = requests.post(
            f"{self.base_url}/orders",
            headers=self.settings.auth_headers(),
            data=json.dumps(payload),
            timeout=20,
        )
        # Surface Dhan's JSON error body if present.
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:  # noqa: BLE001
                detail = resp.text
            raise DhanAPIError(f"Order rejected ({resp.status_code}): {detail}")
        return resp.json()


__all__ = ["DhanClient", "DhanAPIError"]
