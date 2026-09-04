"""Price providers. M1: Finnhub /quote (current + previous close)."""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"


class PriceProvider(ABC):
    @abstractmethod
    def get_quotes(self, tickers: list[str]) -> dict[str, dict[str, float | None]]:
        """Return {ticker: {price, prev_close}} for each requested ticker."""


class FinnhubProvider(PriceProvider):
    def __init__(self, api_key: str | None = None, timeout: float = 10.0) -> None:
        key = os.environ.get("FINNHUB_API_KEY", "") if api_key is None else api_key
        if not key:
            raise RuntimeError("FINNHUB_API_KEY is not set")
        self.api_key = key
        self.timeout = timeout

    def get_quotes(self, tickers: list[str]) -> dict[str, dict[str, float | None]]:
        quotes: dict[str, dict[str, float | None]] = {}
        for ticker in tickers:
            quotes[ticker] = self._fetch_quote(ticker)
        return quotes

    def _fetch_quote(self, ticker: str) -> dict[str, float | None]:
        params = urllib.parse.urlencode({"symbol": ticker, "token": self.api_key})
        url = f"{FINNHUB_QUOTE_URL}?{params}"
        request = urllib.request.Request(
            url, headers={"User-Agent": "stockWatcher-grokbot/m1"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            print(f"skip {ticker}: fetch failed ({exc})", file=sys.stderr)
            return {"price": None, "prev_close": None}

        if not isinstance(payload, dict):
            return {"price": None, "prev_close": None}

        return {
            "price": _optional_float(payload.get("c")),
            "prev_close": _optional_float(payload.get("pc")),
        }


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
