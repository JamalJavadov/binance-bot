import base64
import json
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

import httpx
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.runtime_cache import AsyncTTLCache

logger = get_logger(__name__)


class BinanceAPIError(RuntimeError):
    def __init__(self, raw_message: str) -> None:
        super().__init__(raw_message)
        self.raw_message = raw_message
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            payload = None
        self.payload = payload if isinstance(payload, dict) else None

    @property
    def code(self) -> int | None:
        value = None if self.payload is None else self.payload.get("code")
        return value if isinstance(value, int) else None

    @property
    def exchange_message(self) -> str | None:
        value = None if self.payload is None else self.payload.get("msg")
        return value if isinstance(value, str) and value.strip() else None


@dataclass
class SymbolFilters:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    max_qty: Decimal | None = None
    market_step_size: Decimal | None = None
    market_min_qty: Decimal | None = None
    market_max_qty: Decimal | None = None
    percent_price_multiplier_up: Decimal | None = None
    percent_price_multiplier_down: Decimal | None = None


@dataclass
class LeverageBracket:
    bracket: int
    initial_leverage: int
    notional_cap: Decimal
    notional_floor: Decimal
    maint_margin_ratio: Decimal
    cum: Decimal


class BinanceGateway:
    READ_EXCHANGE_INFO_TTL_SECONDS = 30.0
    READ_MARK_PRICES_TTL_SECONDS = 2.0
    READ_LEVERAGE_BRACKETS_TTL_SECONDS = 10.0
    RISK_ERROR_STREAK_THRESHOLD = 3
    MAX_KLINES_LIMIT = 1500

    def __init__(self) -> None:
        self.settings = get_settings()
        self.client = httpx.AsyncClient(
            base_url=self.settings.binance_base_url,
            timeout=httpx.Timeout(20.0),
        )
        self.server_time_offset_ms = 0
        self._read_cache = AsyncTTLCache()
        self._risk_error_streak = 0
        self._last_risk_error: str | None = None
        self._last_risk_error_at: datetime | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def sync_server_time(self) -> int:
        payload = await self.public_request("GET", "/fapi/v1/time")
        server_time = int(payload["serverTime"])
        self.server_time_offset_ms = server_time - int(time.time() * 1000)
        return self.server_time_offset_ms

    def _build_query_string(self, params: dict) -> str:
        items = [(key, str(value)) for key, value in params.items() if value is not None]
        return urllib.parse.urlencode(items, quote_via=urllib.parse.quote, safe="-_.~")

    def sign_request(self, private_key_pem: str, params: dict) -> str:
        payload = self._build_query_string(params)
        key = load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        signature = key.sign(payload.encode("utf-8"))
        return base64.b64encode(signature).decode("utf-8")

    async def public_request(self, method: str, path: str, params: dict | None = None) -> dict | list:
        response = await self.client.request(method, path, params=params)
        if response.status_code >= 400:
            raise BinanceAPIError(response.text)
        return response.json()

    @staticmethod
    def _is_timestamp_drift_error(error: BinanceAPIError) -> bool:
        return error.code == -1021 or "recvWindow" in error.raw_message

    @staticmethod
    def _is_risk_related_error(error: BinanceAPIError) -> bool:
        if error.code in {-2027, -2028, -2022, -2019, -2018, -2010, -4164}:
            return True
        message = (error.exchange_message or error.raw_message or "").lower()
        return any(
            keyword in message
            for keyword in (
                "insufficient margin",
                "margin is insufficient",
                "risk",
                "reduceonly",
                "reduce only",
                "liquidation",
                "position side",
            )
        )

    def _record_exchange_error(self, error: BinanceAPIError) -> None:
        if not self._is_risk_related_error(error):
            return
        self._risk_error_streak += 1
        self._last_risk_error = error.exchange_message or error.raw_message
        self._last_risk_error_at = datetime.now(timezone.utc)

    def _clear_exchange_error_streak(self) -> None:
        self._risk_error_streak = 0
        self._last_risk_error = None
        self._last_risk_error_at = None

    def risk_error_state(self) -> dict[str, object]:
        return {
            "healthy": self._risk_error_streak < self.RISK_ERROR_STREAK_THRESHOLD,
            "risk_error_streak": self._risk_error_streak,
            "threshold": self.RISK_ERROR_STREAK_THRESHOLD,
            "last_error": self._last_risk_error,
            "last_error_at": None if self._last_risk_error_at is None else self._last_risk_error_at.isoformat(),
        }

    async def _signed_request_once(self, method: str, path: str, *, params: dict, credentials) -> dict | list:
        signed_params = dict(params)
        signed_params["timestamp"] = int(time.time() * 1000 + self.server_time_offset_ms)
        signed_params["recvWindow"] = self.settings.binance_recv_window
        signed_params["signature"] = self.sign_request(credentials.private_key_pem, signed_params)
        headers = {"X-MBX-APIKEY": credentials.api_key}
        response = await self.client.request(method, path, params=signed_params, headers=headers)
        if response.status_code >= 400:
            raise BinanceAPIError(response.text)
        return response.json()

    async def signed_request(self, method: str, path: str, *, params: dict, credentials) -> dict | list:
        try:
            payload = await self._signed_request_once(method, path, params=params, credentials=credentials)
            self._clear_exchange_error_streak()
            return payload
        except BinanceAPIError as exc:
            if not self._is_timestamp_drift_error(exc):
                self._record_exchange_error(exc)
                raise

            logger.warning("binance_gateway.timestamp_resync", path=path)
            await self.sync_server_time()
            try:
                payload = await self._signed_request_once(method, path, params=params, credentials=credentials)
                self._clear_exchange_error_streak()
                return payload
            except BinanceAPIError as retry_exc:
                self._record_exchange_error(retry_exc)
                raise

    async def api_key_request(
        self,
        method: str,
        path: str,
        *,
        credentials,
        params: dict | None = None,
    ) -> dict | list:
        headers = {"X-MBX-APIKEY": credentials.api_key}
        response = await self.client.request(method, path, params=params or {}, headers=headers)
        if response.status_code >= 400:
            raise BinanceAPIError(response.text)
        return response.json()

    async def ping(self) -> bool:
        try:
            await self.public_request("GET", "/fapi/v1/ping")
            return True
        except Exception:
            return False

    async def exchange_info(self) -> dict:
        return await self.public_request("GET", "/fapi/v1/exchangeInfo")

    async def read_cached_exchange_info(self) -> dict:
        return await self._read_cache.get_or_set(
            "exchange_info",
            ttl_seconds=self.READ_EXCHANGE_INFO_TTL_SECONDS,
            factory=self.exchange_info,
        )

    async def top_symbols_by_volume(self, *, limit: int) -> list[str]:
        tickers = await self.ticker_24hr()
        filtered = [
            row
            for row in tickers
            if row["symbol"].endswith("USDT")
            and all(stable not in row["symbol"] for stable in ("BUSD", "USDC", "TUSD", "FDUSD"))
        ]
        filtered.sort(key=lambda row: Decimal(row["quoteVolume"]), reverse=True)
        return [row["symbol"] for row in filtered[:limit]]

    async def top_symbols_by_liquidity(
        self,
        *,
        limit: int,
        min_quote_volume: Decimal,
        max_spread_bps: Decimal,
    ) -> list[str]:
        tickers = await self.ticker_24hr()
        book_tickers = await self.book_tickers()
        filtered: list[dict] = []
        for row in tickers:
            symbol = row.get("symbol")
            if not isinstance(symbol, str):
                continue
            if not symbol.endswith("USDT") or any(stable in symbol for stable in ("BUSD", "USDC", "TUSD", "FDUSD")):
                continue
            quote_volume = Decimal(str(row.get("quoteVolume", "0")))
            if quote_volume < min_quote_volume:
                continue
            book = book_tickers.get(symbol, {})
            bid = Decimal(str(book.get("bidPrice", "0")))
            ask = Decimal(str(book.get("askPrice", "0")))
            mid = (bid + ask) / Decimal("2") if bid > 0 and ask > 0 else Decimal("0")
            if mid <= 0:
                continue
            spread_bps = ((ask - bid) / mid) * Decimal("10000")
            if spread_bps > max_spread_bps:
                continue
            filtered.append({"symbol": symbol, "quoteVolume": quote_volume})
        filtered.sort(key=lambda row: row["quoteVolume"], reverse=True)
        return [str(row["symbol"]) for row in filtered[:limit]]

    async def ticker_24hr(self) -> list[dict]:
        payload = await self.public_request("GET", "/fapi/v1/ticker/24hr")
        return payload if isinstance(payload, list) else []

    async def book_tickers(self) -> dict[str, dict]:
        payload = await self.public_request("GET", "/fapi/v1/ticker/bookTicker")
        if not isinstance(payload, list):
            return {}
        return {
            item["symbol"]: item
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("symbol"), str)
        }

    async def klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list]:
        params: dict[str, str | int] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        payload = await self.public_request(
            "GET",
            "/fapi/v1/klines",
            params=params,
        )
        return payload if isinstance(payload, list) else []

    async def klines_history(self, symbol: str, interval: str, limit: int) -> list[list]:
        requested_limit = max(int(limit), 0)
        if requested_limit <= 0:
            return []
        if requested_limit <= self.MAX_KLINES_LIMIT:
            return await self.klines(symbol, interval, requested_limit)

        batches: list[list[list]] = []
        remaining = requested_limit
        end_time: int | None = None
        while remaining > 0:
            batch_limit = min(self.MAX_KLINES_LIMIT, remaining)
            batch = await self.klines(symbol, interval, batch_limit, end_time=end_time)
            if not batch:
                break
            batches.append(batch)
            remaining -= len(batch)
            if len(batch) < batch_limit:
                break
            try:
                first_open_time = int(batch[0][0])
            except (TypeError, ValueError, IndexError):
                break
            end_time = first_open_time - 1

        flattened: list[list] = []
        for batch in reversed(batches):
            flattened.extend(batch)
        return flattened[-requested_limit:]

    async def mark_price(self, symbol: str) -> dict:
        return await self.public_request("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol})

    async def mark_prices(self) -> dict[str, dict]:
        payload = await self.public_request("GET", "/fapi/v1/premiumIndex")
        if not isinstance(payload, list):
            return {}
        return {
            item["symbol"]: item
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("symbol"), str)
        }

    async def read_cached_mark_prices(self) -> dict[str, dict]:
        return await self._read_cache.get_or_set(
            "mark_prices",
            ttl_seconds=self.READ_MARK_PRICES_TTL_SECONDS,
            factory=self.mark_prices,
        )

    async def account_balance(self, credentials) -> list[dict]:
        return await self.signed_request("GET", "/fapi/v3/balance", params={}, credentials=credentials)

    async def account_info(self, credentials) -> dict:
        payload = await self.signed_request("GET", "/fapi/v3/account", params={}, credentials=credentials)
        return payload if isinstance(payload, dict) else {}

    async def positions(self, credentials) -> list[dict]:
        return await self.signed_request("GET", "/fapi/v3/positionRisk", params={}, credentials=credentials)

    async def start_user_data_stream(self, credentials) -> str:
        payload = await self.api_key_request(
            "POST",
            "/fapi/v1/listenKey",
            credentials=credentials,
        )
        listen_key = payload.get("listenKey") if isinstance(payload, dict) else None
        if not isinstance(listen_key, str) or not listen_key.strip():
            raise ValueError("Binance user data stream did not return a listenKey")
        return listen_key

    async def keepalive_user_data_stream(self, credentials, listen_key: str) -> dict:
        payload = await self.api_key_request(
            "PUT",
            "/fapi/v1/listenKey",
            credentials=credentials,
            params={"listenKey": listen_key},
        )
        return payload if isinstance(payload, dict) else {}

    async def close_user_data_stream(self, credentials, listen_key: str) -> dict:
        payload = await self.api_key_request(
            "DELETE",
            "/fapi/v1/listenKey",
            credentials=credentials,
            params={"listenKey": listen_key},
        )
        return payload if isinstance(payload, dict) else {}

    def user_data_stream_ws_url(self, listen_key: str) -> str:
        base = urllib.parse.urlparse(self.settings.binance_base_url)
        ws_scheme = "wss" if base.scheme == "https" else "ws"
        return f"{ws_scheme}://{base.netloc}/ws/{listen_key}"

    async def leverage_brackets(self, credentials, symbol: str | None = None) -> dict[str, list[LeverageBracket]]:
        params = {"symbol": symbol} if symbol else {}
        payload = await self.signed_request("GET", "/fapi/v1/leverageBracket", params=params, credentials=credentials)
        return self.parse_leverage_brackets(payload)

    async def read_cached_leverage_brackets(self, credentials) -> dict[str, list[LeverageBracket]]:
        credential_key = getattr(credentials, "api_key", "default")
        return await self._read_cache.get_or_set(
            f"leverage_brackets:{credential_key}",
            ttl_seconds=self.READ_LEVERAGE_BRACKETS_TTL_SECONDS,
            factory=lambda: self.leverage_brackets(credentials),
        )

    async def change_margin_type(self, credentials, symbol: str, margin_type: str = "ISOLATED") -> dict:
        try:
            return await self.signed_request(
                "POST",
                "/fapi/v1/marginType",
                params={"symbol": symbol, "marginType": margin_type},
                credentials=credentials,
            )
        except BinanceAPIError as exc:
            if "No need to change margin type" in str(exc):
                return {"msg": "unchanged"}
            raise

    async def get_position_mode(self, credentials) -> bool:
        payload = await self.signed_request("GET", "/fapi/v1/positionSide/dual", params={}, credentials=credentials)
        value = payload.get("dualSidePosition") if isinstance(payload, dict) else None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() == "true"
        raise ValueError("Binance position mode response did not include dualSidePosition")

    async def change_position_mode(self, credentials, dual_side: bool = False) -> dict:
        try:
            return await self.signed_request(
                "POST",
                "/fapi/v1/positionSide/dual",
                params={"dualSidePosition": str(dual_side).lower()},
                credentials=credentials,
            )
        except BinanceAPIError as exc:
            if "No need to change position side" in str(exc):
                return {"msg": "unchanged"}
            raise

    async def change_leverage(self, credentials, symbol: str, leverage: int) -> dict:
        return await self.signed_request(
            "POST",
            "/fapi/v1/leverage",
            params={"symbol": symbol, "leverage": leverage},
            credentials=credentials,
        )

    async def commission_rate(self, credentials, symbol: str) -> dict:
        payload = await self.signed_request(
            "GET",
            "/fapi/v1/commissionRate",
            params={"symbol": symbol},
            credentials=credentials,
        )
        return payload if isinstance(payload, dict) else {}

    async def place_order(self, credentials, params: dict) -> dict:
        return await self.signed_request("POST", "/fapi/v1/order", params=params, credentials=credentials)

    async def place_algo_order(self, credentials, params: dict) -> dict:
        return await self.signed_request("POST", "/fapi/v1/algoOrder", params=params, credentials=credentials)

    async def query_order(
        self,
        credentials,
        symbol: str,
        order_id: str | None,
        *,
        orig_client_order_id: str | None = None,
    ) -> dict:
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        return await self.signed_request(
            "GET",
            "/fapi/v1/order",
            params=params,
            credentials=credentials,
        )

    async def query_algo_order(self, credentials, algo_id: str | None, *, client_algo_id: str | None = None) -> dict:
        params: dict[str, str] = {}
        if algo_id is not None:
            params["algoId"] = algo_id
        if client_algo_id is not None:
            params["clientAlgoId"] = client_algo_id
        return await self.signed_request(
            "GET",
            "/fapi/v1/algoOrder",
            params=params,
            credentials=credentials,
        )

    async def cancel_order(self, credentials, symbol: str, order_id: str) -> dict:
        return await self.signed_request(
            "DELETE",
            "/fapi/v1/order",
            params={"symbol": symbol, "orderId": order_id},
            credentials=credentials,
        )

    async def cancel_algo_order(self, credentials, algo_id: str) -> dict:
        return await self.signed_request(
            "DELETE",
            "/fapi/v1/algoOrder",
            params={"algoId": algo_id},
            credentials=credentials,
        )

    async def open_orders(self, credentials, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        payload = await self.signed_request("GET", "/fapi/v1/openOrders", params=params, credentials=credentials)
        return payload if isinstance(payload, list) else []

    async def all_orders(
        self,
        credentials,
        symbol: str,
        *,
        order_id: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        params: dict[str, str | int] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        if limit is not None:
            params["limit"] = limit
        payload = await self.signed_request("GET", "/fapi/v1/allOrders", params=params, credentials=credentials)
        return payload if isinstance(payload, list) else []

    async def open_algo_orders(self, credentials, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        payload = await self.signed_request("GET", "/fapi/v1/openAlgoOrders", params=params, credentials=credentials)
        return payload if isinstance(payload, list) else []

    async def all_algo_orders(
        self,
        credentials,
        symbol: str,
        *,
        algo_id: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        page: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        params: dict[str, str | int] = {"symbol": symbol}
        if algo_id is not None:
            params["algoId"] = algo_id
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        if page is not None:
            params["page"] = page
        if limit is not None:
            params["limit"] = limit
        payload = await self.signed_request("GET", "/fapi/v1/allAlgoOrders", params=params, credentials=credentials)
        return payload if isinstance(payload, list) else []

    async def account_trades(
        self,
        credentials,
        symbol: str,
        *,
        order_id: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        from_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        params: dict[str, str | int] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        if from_id is not None:
            params["fromId"] = from_id
        if limit is not None:
            params["limit"] = limit
        payload = await self.signed_request("GET", "/fapi/v1/userTrades", params=params, credentials=credentials)
        return payload if isinstance(payload, list) else []

    async def funding_rate_history(self, symbol: str, *, limit: int = 12) -> list[dict]:
        payload = await self.public_request(
            "GET",
            "/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": limit},
        )
        return payload if isinstance(payload, list) else []

    def parse_symbol_filters(self, exchange_info: dict) -> dict[str, SymbolFilters]:
        mapping: dict[str, SymbolFilters] = {}
        for symbol_info in exchange_info.get("symbols", []):
            by_type = {flt["filterType"]: flt for flt in symbol_info.get("filters", [])}
            lot = by_type.get("LOT_SIZE", {})
            market_lot = by_type.get("MARKET_LOT_SIZE", {})
            price_filter = by_type.get("PRICE_FILTER", {})
            notional_filter = by_type.get("MIN_NOTIONAL", {})
            percent_price_filter = by_type.get("PERCENT_PRICE", {})

            lot_max_qty = Decimal(str(lot.get("maxQty", "0")))
            market_step_size = Decimal(str(market_lot.get("stepSize", "0")))
            market_min_qty = Decimal(str(market_lot.get("minQty", "0")))
            market_max_qty = Decimal(str(market_lot.get("maxQty", "0")))
            percent_multiplier_up = Decimal(str(percent_price_filter.get("multiplierUp", "0")))
            percent_multiplier_down = Decimal(str(percent_price_filter.get("multiplierDown", "0")))
            mapping[symbol_info["symbol"]] = SymbolFilters(
                symbol=symbol_info["symbol"],
                tick_size=Decimal(price_filter.get("tickSize", "0.01")),
                step_size=Decimal(lot.get("stepSize", "0.001")),
                min_qty=Decimal(lot.get("minQty", "0.001")),
                min_notional=Decimal(notional_filter.get("notional", "5")),
                max_qty=lot_max_qty if lot_max_qty > 0 else None,
                market_step_size=market_step_size if market_step_size > 0 else None,
                market_min_qty=market_min_qty if market_min_qty > 0 else None,
                market_max_qty=market_max_qty if market_max_qty > 0 else None,
                percent_price_multiplier_up=percent_multiplier_up if percent_multiplier_up > 0 else None,
                percent_price_multiplier_down=percent_multiplier_down if percent_multiplier_down > 0 else None,
            )
        return mapping

    def parse_leverage_brackets(self, payload: list[dict] | dict) -> dict[str, list[LeverageBracket]]:
        rows = payload if isinstance(payload, list) else []
        mapping: dict[str, list[LeverageBracket]] = {}
        for item in rows:
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                continue
            brackets: list[LeverageBracket] = []
            for bracket in item.get("brackets", []):
                brackets.append(
                    LeverageBracket(
                        bracket=int(bracket.get("bracket", 0)),
                        initial_leverage=int(bracket.get("initialLeverage", 1)),
                        notional_cap=Decimal(str(bracket.get("notionalCap", "0"))),
                        notional_floor=Decimal(str(bracket.get("notionalFloor", "0"))),
                        maint_margin_ratio=Decimal(str(bracket.get("maintMarginRatio", "0"))),
                        cum=Decimal(str(bracket.get("cum", "0"))),
                    )
                )
            brackets.sort(key=lambda row: (row.notional_floor, row.notional_cap, row.bracket))
            mapping[symbol] = brackets
        return mapping


def round_to_increment(value: Decimal, increment: Decimal, *, rounding=ROUND_DOWN) -> Decimal:
    if increment == 0:
        return value
    return (value / increment).to_integral_value(rounding=rounding) * increment
