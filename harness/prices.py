"""
Price data layer.

The rest of the system never knows which data source it's talking to.
It just calls get_price_source(track) and asks for prices.

Sources
-------
  CoinGeckoSource  -- free public API, no key required, for crypto assets
  YFinanceSource   -- yfinance wrapper for equities

To add a paid provider later: subclass PriceSource, implement get_price()
and optionally get_current_price() / get_price_range(), then swap the
factory function get_price_source().

Sanity check
------------
sanity_check() is a hard gate that raises BadPriceData before any bad
number can contaminate a resolution score.  Checks:
  - price must be positive and finite
  - if a prior-day price is supplied, single-day move must be <= max_move
    (default 90%) -- catches stale/replayed/glitched data
"""

import math
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import requests

try:
    import yfinance as yf
    _HAS_YFINANCE = True
except ImportError:
    _HAS_YFINANCE = False


class BadPriceData(Exception):
    """Raised when fetched price data fails sanity checks."""


# ------------------------------------------------------------------
# Abstract interface
# ------------------------------------------------------------------

class PriceSource(ABC):

    @abstractmethod
    def get_price(self, asset: str, on_date: date) -> float:
        """Return the closing/daily price of asset on the given date (USD)."""

    def get_current_price(self, asset: str) -> float:
        """Return the latest available price.  Override for efficiency."""
        return self.get_price(asset, date.today())

    def get_price_range(self, asset: str, start: date, end: date) -> List[Tuple[date, float]]:
        """
        Return a list of (date, price) pairs over [start, end] inclusive.
        Default implementation iterates day-by-day; subclasses override
        with bulk-fetch endpoints.
        """
        results: List[Tuple[date, float]] = []
        current = start
        while current <= end:
            try:
                price = self.get_price(asset, current)
                results.append((current, price))
            except Exception:
                pass  # skip non-trading days / missing data
            current += timedelta(days=1)
        return results


# ------------------------------------------------------------------
# CoinGecko (crypto)
# ------------------------------------------------------------------

class CoinGeckoSource(PriceSource):
    """
    Uses the free CoinGecko public API (v3).  No API key required.
    Rate limit: ~10-30 req/min on the free tier; retries once on 429.
    """

    BASE = "https://api.coingecko.com/api/v3"

    # Common ticker -> CoinGecko coin ID.  Assets not in this map are
    # passed through as-is (lowercase), so full coin IDs also work.
    SYMBOL_MAP: dict = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
        "BNB": "binancecoin",
        "ADA": "cardano",
        "XRP": "ripple",
        "DOT": "polkadot",
        "AVAX": "avalanche-2",
        "MATIC": "matic-network",
        "LINK": "chainlink",
        "UNI": "uniswap",
        "ATOM": "cosmos",
        "DOGE": "dogecoin",
        "LTC": "litecoin",
        "NEAR": "near",
        "ARB": "arbitrum",
        "OP": "optimism",
    }

    def _coin_id(self, asset: str) -> str:
        return self.SYMBOL_MAP.get(asset.upper(), asset.lower())

    def _get(self, path: str, params: dict) -> dict:
        resp = requests.get(f"{self.BASE}{path}", params=params, timeout=15)
        if resp.status_code == 429:
            time.sleep(65)  # free tier: wait out the rate limit window
            resp = requests.get(f"{self.BASE}{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_price(self, asset: str, on_date: date) -> float:
        data = self._get(
            f"/coins/{self._coin_id(asset)}/history",
            {"date": on_date.strftime("%d-%m-%Y"), "localization": "false"},
        )
        try:
            return float(data["market_data"]["current_price"]["usd"])
        except (KeyError, TypeError) as exc:
            raise BadPriceData(
                f"CoinGecko: no price for {asset} on {on_date}. "
                f"Response keys: {list(data.keys())}"
            ) from exc

    def get_current_price(self, asset: str) -> float:
        coin_id = self._coin_id(asset)
        data = self._get("/simple/price", {"ids": coin_id, "vs_currencies": "usd"})
        try:
            return float(data[coin_id]["usd"])
        except (KeyError, TypeError) as exc:
            raise BadPriceData(
                f"CoinGecko: no current price for {asset}. Response: {data}"
            ) from exc

    def get_price_range(self, asset: str, start: date, end: date) -> List[Tuple[date, float]]:
        """Uses market_chart/range for a single API call instead of N daily calls."""
        from_ts = int(datetime.combine(start, datetime.min.time()).timestamp())
        to_ts = int(datetime.combine(end, datetime.max.time()).timestamp())
        data = self._get(
            f"/coins/{self._coin_id(asset)}/market_chart/range",
            {"vs_currency": "usd", "from": from_ts, "to": to_ts},
        )
        seen: set = set()
        results: List[Tuple[date, float]] = []
        for ts_ms, price in data.get("prices", []):
            d = datetime.utcfromtimestamp(ts_ms / 1000).date()
            if d not in seen:
                seen.add(d)
                results.append((d, float(price)))
        return sorted(results)

    def get_prices_in_window(self, asset: str, start: date, end: date) -> List[float]:
        """
        Return ALL price points (not deduplicated to daily) for the given window.

        CoinGecko granularity via market_chart/range:
          <= 1 day  : 5-minute data
          2-90 days : hourly data
          91+ days  : daily data

        Used for liquidation checking during resolution -- we need every
        available data point to catch intraday touches of the liquidation level.

        Limitation: sub-hourly wicks in windows <= 90 days may not be captured
        (hourly is the finest freely-available granularity).  Labeled in
        resolution notes when used.
        """
        from_ts = int(datetime.combine(start, datetime.min.time()).timestamp())
        to_ts = int(datetime.combine(end, datetime.max.time()).timestamp())
        data = self._get(
            f"/coins/{self._coin_id(asset)}/market_chart/range",
            {"vs_currency": "usd", "from": from_ts, "to": to_ts},
        )
        return [float(p) for _, p in data.get("prices", [])]


# ------------------------------------------------------------------
# yfinance (equities)
# ------------------------------------------------------------------

class YFinanceSource(PriceSource):

    def _require_yf(self) -> None:
        if not _HAS_YFINANCE:
            raise ImportError("yfinance not installed.  Run: pip install yfinance")

    def get_price(self, asset: str, on_date: date) -> float:
        self._require_yf()
        ticker = yf.Ticker(asset.upper())
        # Request a small window past the target date to handle weekends/holidays.
        hist = ticker.history(
            start=on_date.isoformat(),
            end=(on_date + timedelta(days=5)).isoformat(),
        )
        if hist.empty:
            raise BadPriceData(
                f"yfinance: no data for {asset} on or after {on_date}"
            )
        return float(hist["Close"].iloc[0])

    def get_current_price(self, asset: str) -> float:
        self._require_yf()
        hist = yf.Ticker(asset.upper()).history(period="1d")
        if hist.empty:
            raise BadPriceData(f"yfinance: no current data for {asset}")
        return float(hist["Close"].iloc[-1])

    def get_price_range(self, asset: str, start: date, end: date) -> List[Tuple[date, float]]:
        self._require_yf()
        hist = yf.Ticker(asset.upper()).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),  # yfinance end is exclusive
        )
        if hist.empty:
            return []
        return [(row.Index.date(), float(row.Close)) for row in hist.itertuples()]


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------

def get_price_source(track: str) -> PriceSource:
    if track == "crypto":
        return CoinGeckoSource()
    if track == "equities":
        return YFinanceSource()
    raise ValueError(f"Unknown track {track!r}.  Must be 'crypto' or 'equities'.")


# ------------------------------------------------------------------
# Sanity check -- hard gate before any price touches scoring
# ------------------------------------------------------------------

def sanity_check(
    price: float,
    asset: str,
    on_date: date,
    prior_price: Optional[float] = None,
    max_move: float = 0.90,
) -> None:
    """
    Raises BadPriceData if the price looks glitched.

    Checks performed:
      1. Price must not be None, NaN, or infinite.
      2. Price must be strictly positive.
      3. If prior_price is provided: single-period move must be <= max_move
         (default 90%).  A 90 %+ move in one day almost certainly means
         stale replay, a ticker swap, or a data feed error.

    Call this before using any fetched price to score a prediction.
    """
    if price is None or not isinstance(price, (int, float)):
        raise BadPriceData(
            f"Price for {asset} on {on_date} is not a number: {price!r}"
        )
    if math.isnan(price) or math.isinf(price):
        raise BadPriceData(
            f"Price for {asset} on {on_date} is NaN or infinite: {price}"
        )
    if price <= 0:
        raise BadPriceData(
            f"Price for {asset} on {on_date} is non-positive: {price}"
        )
    if prior_price is not None and prior_price > 0:
        change = abs(price - prior_price) / prior_price
        if change > max_move:
            raise BadPriceData(
                f"Suspicious {change:.0%} move for {asset} on {on_date}: "
                f"{prior_price:.6g} -> {price:.6g}.  "
                f"Exceeds {max_move:.0%} limit -- possible data glitch.  "
                f"Refusing to score this prediction."
            )
