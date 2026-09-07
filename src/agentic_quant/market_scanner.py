from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
import yaml

from agentic_quant.archive import RawArchive
from agentic_quant.config import Settings
from agentic_quant.control_plane import SystemObjectStore
from agentic_quant.database import ledger_events
from agentic_quant.domain import EventEnvelope, LLMInvocationStatus, LLMWorkload
from agentic_quant.ids import stable_uuid
from agentic_quant.ledger import EventLedger
from agentic_quant.llm import (
    LLMConfigurationError,
    LLMGateway,
    LLMProviderError,
    LLMRequest,
)
from agentic_quant.llm_budget import LLMBudgetExceededError
from agentic_quant.market_store import MarketDataStore
from agentic_quant.providers.alpaca import AlpacaMarketDataProvider
from agentic_quant.providers.base import (
    AssetCatalogPage,
    MarketScreenerPage,
    StockSnapshotsPage,
)
from agentic_quant.risk import RestrictionRegistry


MARKET_SCAN_EVENT = "market.universe.scanned.v1"
MARKET_SCAN_PROMPT_VERSION = "market_scanner_rerank@0.1.0"


class MarketScannerPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str = Field(pattern=r"^market_scanner@[0-9]+\.[0-9]+\.[0-9]+$")
    limits: dict[str, int | str]
    excluded_asset_name_terms: tuple[str, ...]
    themes: dict[str, tuple[str, ...]]

    @model_validator(mode="after")
    def validate_policy(self) -> MarketScannerPolicy:
        integer_limits = {
            "most_active_count": (1, 100),
            "mover_count": (1, 50),
            "snapshot_batch_size": (1, 200),
            "deterministic_review_count": (1, 100),
            "selected_count": (1, 50),
            "llm_max_output_tokens": (128, 4_096),
            "llm_min_interval_minutes": (1, 1_440),
        }
        for name, (minimum, maximum) in integer_limits.items():
            value = int(self.limits.get(name, 0))
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if int(self.limits["selected_count"]) > int(
            self.limits["deterministic_review_count"]
        ):
            raise ValueError("selected_count cannot exceed deterministic_review_count")
        for name in ("minimum_price_usd", "minimum_dollar_volume_usd"):
            if Decimal(str(self.limits.get(name, "0"))) <= 0:
                raise ValueError(f"{name} must be positive")
        symbols = [symbol for values in self.themes.values() for symbol in values]
        normalized = [symbol.strip().upper() for symbol in symbols]
        if len(normalized) != len(set(normalized)):
            raise ValueError("A market-scanner seed symbol may belong to only one theme")
        if any(not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,23}", value) for value in normalized):
            raise ValueError("Market-scanner themes contain an invalid symbol")
        return self

    def integer(self, name: str) -> int:
        return int(self.limits[name])

    def decimal(self, name: str) -> Decimal:
        return Decimal(str(self.limits[name]))

    @property
    def seed_themes(self) -> dict[str, str]:
        return {
            symbol.upper(): theme
            for theme, symbols in self.themes.items()
            for symbol in symbols
        }


class MarketScanCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,23}$")
    deterministic_rank: int = Field(ge=1)
    final_rank: int = Field(ge=1)
    deterministic_score: Decimal = Field(ge=0)
    final_score: Decimal = Field(ge=0)
    attention_class: Literal["HOT", "EMERGING", "LIQUID_CORE"]
    selected: bool
    metrics: dict[str, Any]
    source_tags: tuple[str, ...]
    theme: str | None = None
    deterministic_reasons: tuple[str, ...]
    llm_priority_score: int | None = Field(default=None, ge=0, le=10)
    llm_thesis: str | None = Field(default=None, max_length=500)
    llm_risks: tuple[str, ...] = ()


class _LLMPick(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,23}$")
    priority_score: int = Field(ge=0, le=10)
    attention_class: Literal["HOT", "EMERGING", "LIQUID_CORE"]
    thesis: str = Field(min_length=1, max_length=500)
    risks: tuple[str, ...] = Field(default=(), max_length=5)


class _LLMSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    picks: tuple[_LLMPick, ...] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def symbols_are_unique(self) -> _LLMSelection:
        symbols = [item.symbol for item in self.picks]
        if len(symbols) != len(set(symbols)):
            raise ValueError("LLM scanner output contains duplicate symbols")
        return self


class MarketScannerProvider(Protocol):
    async def __aenter__(self) -> MarketScannerProvider: ...

    async def __aexit__(self, *_args: object) -> None: ...

    async def fetch_most_actives(self, *, top: int) -> MarketScreenerPage: ...

    async def fetch_market_movers(self, *, top: int) -> MarketScreenerPage: ...

    async def fetch_active_assets(self) -> AssetCatalogPage: ...

    async def fetch_stock_snapshots(
        self,
        *,
        symbols: tuple[str, ...],
        feed: str,
    ) -> StockSnapshotsPage: ...


def load_market_scanner_policy(path: Path) -> MarketScannerPolicy:
    return MarketScannerPolicy.model_validate(yaml.safe_load(path.read_text()))


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _json_object(output: str) -> dict[str, Any]:
    candidate = output.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1]
        if candidate.endswith("```"):
            candidate = candidate[:-3]
    try:
        parsed = json.loads(candidate)
        if not isinstance(parsed, dict):
            raise ValueError("LLM scanner response must be a JSON object")
        return parsed
    except json.JSONDecodeError as original_error:
        # Some Responses-compatible models expose a draft before their final JSON. Keep
        # strict schema validation, but recover the last complete top-level picks object.
        decoder = json.JSONDecoder()
        recovered: dict[str, Any] | None = None
        for match in re.finditer(r"\{", candidate):
            try:
                value, _ = decoder.raw_decode(candidate[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "picks" in value:
                recovered = value
        if recovered is None:
            raise original_error
        return recovered


class MarketScanStore:
    def __init__(self, ledger: EventLedger) -> None:
        self.ledger = ledger
        self.engine = ledger.engine

    def get(self, scan_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(ledger_events)
                .where(ledger_events.c.event_type == MARKET_SCAN_EVENT)
                .where(ledger_events.c.correlation_id == scan_id)
                .order_by(ledger_events.c.sequence.desc())
                .limit(1)
            ).one_or_none()
        return self._serialize(row) if row is not None else None

    def recent(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(ledger_events)
                .where(ledger_events.c.event_type == MARKET_SCAN_EVENT)
                .order_by(ledger_events.c.sequence.desc())
                .limit(limit)
            ).all()
        return [self._serialize(row) for row in rows]

    def latest(self) -> dict[str, Any] | None:
        values = self.recent(limit=1)
        return values[0] if values else None

    def record(self, *, scan_id: str, as_of: datetime, payload: dict[str, Any]) -> None:
        self.ledger.append(
            EventEnvelope(
                event_id=scan_id,
                event_type=MARKET_SCAN_EVENT,
                event_time=as_of,
                emitted_at=datetime.now(UTC),
                producer="market-universe-scanner",
                correlation_id=scan_id,
                payload=payload,
            )
        )

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        value = dict(row._mapping)
        payload = dict(value.pop("payload"))
        payload["sequence"] = int(value["sequence"])
        payload["recorded_at"] = value["emitted_at"]
        return payload


class MarketUniverseScanner:
    """Discover attention candidates without granting execution authority."""

    def __init__(
        self,
        *,
        settings: Settings,
        ledger: EventLedger,
        objects: SystemObjectStore,
        market: MarketDataStore,
        archive: RawArchive,
        llm_gateway: LLMGateway,
        restrictions: RestrictionRegistry,
        provider_factory: Callable[[], MarketScannerProvider] | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.objects = objects
        self.market = market
        self.archive = archive
        self.llm_gateway = llm_gateway
        self.restrictions = restrictions
        self.policy = load_market_scanner_policy(
            settings.market_scanner_policy_path
        )
        self.store = MarketScanStore(ledger)
        self.provider_factory = provider_factory

    def status(self, *, limit: int = 5) -> dict[str, Any]:
        return {
            "enabled": self.settings.market_scanner_enabled,
            "llm_enabled": self.settings.market_scanner_llm_enabled,
            "policy_version": self.policy.version,
            "limits": dict(self.policy.limits),
            "theme_count": len(self.policy.themes),
            "seed_count": len(self.policy.seed_themes),
            "latest_run": self.store.latest(),
            "recent_runs": self.store.recent(limit=limit),
            "execution_authority": False,
        }

    async def run_once(self, *, as_of: datetime) -> dict[str, Any]:
        if as_of.tzinfo is None:
            raise ValueError("Market scan cutoff must be timezone-aware")
        cutoff = as_of.astimezone(UTC)
        cycle_key = cutoff.strftime("%Y-%m-%dT%H")
        scan_id = stable_uuid(
            "market-universe-scan",
            self.settings.deployment_environment_id,
            self.policy.version,
            cycle_key,
        )
        existing = self.store.get(scan_id)
        if existing is not None:
            return {**existing, "reused": True}
        if self.settings.alpaca_api_key is None or self.settings.alpaca_api_secret is None:
            return self._record_failure(
                scan_id=scan_id,
                as_of=cutoff,
                error_code="WAITING_ALPACA_CREDENTIALS",
            )
        try:
            raw_object_ids, source_rows, snapshots, assets = await self._fetch_sources()
            candidates = self._rank_candidates(
                source_rows=source_rows,
                snapshots=snapshots,
                assets=assets,
                as_of=cutoff,
            )
        except Exception as exc:
            return self._record_failure(
                scan_id=scan_id,
                as_of=cutoff,
                error_code=type(exc).__name__,
            )
        llm_status = "DISABLED"
        llm_invocation_id: str | None = None
        llm_error_code: str | None = None
        if self.settings.market_scanner_llm_enabled and candidates:
            if self._llm_interval_elapsed(cutoff):
                try:
                    (
                        candidates,
                        llm_invocation_id,
                        llm_error_code,
                    ) = await self._llm_rerank(candidates)
                    llm_status = (
                        "FAILED_FALLBACK"
                        if llm_error_code is not None
                        else "COMPLETED"
                    )
                except (
                    LLMBudgetExceededError,
                    LLMConfigurationError,
                    LLMProviderError,
                ) as exc:
                    llm_status = "FAILED_FALLBACK"
                    llm_error_code = type(exc).__name__
            else:
                llm_status = "SKIPPED_INTERVAL"
        selected_count = min(self.policy.integer("selected_count"), len(candidates))
        finalized = [
            item.model_copy(
                update={"selected": index < selected_count, "final_rank": index + 1}
            )
            for index, item in enumerate(candidates)
        ]
        selected_symbols = [item.symbol for item in finalized if item.selected]
        updated_list = self.objects.replace_list_members(
            slug_or_id="candidate-list",
            members=selected_symbols,
            reason=f"Automated {self.policy.version} scan {scan_id}",
            created_by="market-universe-scanner",
        )
        payload = {
            "scan_id": scan_id,
            "cycle_key": cycle_key,
            "as_of": cutoff.isoformat(),
            "status": "COMPLETED",
            "policy_version": self.policy.version,
            "source_symbol_count": len(source_rows),
            "eligible_count": len(finalized),
            "selected_count": len(selected_symbols),
            "selected_symbols": selected_symbols,
            "candidate_list_revision": updated_list["current_revision"],
            "raw_object_ids": raw_object_ids,
            "llm_status": llm_status,
            "llm_invocation_id": llm_invocation_id,
            "llm_error_code": llm_error_code,
            "candidates": [item.model_dump(mode="json") for item in finalized],
            "automatic_execution": False,
        }
        self.store.record(scan_id=scan_id, as_of=cutoff, payload=payload)
        return payload

    async def _fetch_sources(
        self,
    ) -> tuple[
        list[str],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        if self.provider_factory is not None:
            provider = self.provider_factory()
        else:
            api_key = self.settings.alpaca_api_key
            api_secret = self.settings.alpaca_api_secret
            assert api_key is not None and api_secret is not None
            provider = AlpacaMarketDataProvider(
                api_key=api_key.get_secret_value(),
                api_secret=api_secret.get_secret_value(),
                base_url=self.settings.alpaca_data_base_url,
                assets_base_url=self.settings.alpaca_paper_base_url,
                calendar_name=self.settings.market_calendar,
            )
        async with provider:
            active_page = await provider.fetch_most_actives(
                top=self.policy.integer("most_active_count")
            )
            mover_page = await provider.fetch_market_movers(
                top=self.policy.integer("mover_count")
            )
            asset_page = await provider.fetch_active_assets()
            raw_object_ids = [
                self._archive_page(active_page),
                self._archive_page(mover_page),
                self._archive_page(asset_page),
            ]
            source_rows = self._source_rows(active_page, mover_page)
            focus = self.objects.get_list("focus-watchlist")
            for symbol in self.policy.seed_themes:
                source_rows.setdefault(symbol, {"source_tags": set()})[
                    "source_tags"
                ].add("THEME_SEED")
            for symbol in (focus["members"] if focus else []):
                source_rows.setdefault(str(symbol), {"source_tags": set()})[
                    "source_tags"
                ].add("FOCUS_WATCHLIST")
            symbols = tuple(sorted(source_rows))
            snapshots: dict[str, dict[str, Any]] = {}
            batch_size = self.policy.integer("snapshot_batch_size")
            for offset in range(0, len(symbols), batch_size):
                page = await provider.fetch_stock_snapshots(
                    symbols=symbols[offset : offset + batch_size],
                    feed=self.settings.alpaca_stock_feed,
                )
                raw_object_ids.append(self._archive_page(page))
                raw = page.raw_payload.get("snapshots") or page.raw_payload
                snapshots.update(
                    {
                        str(symbol).upper(): dict(value)
                        for symbol, value in raw.items()
                        if isinstance(value, dict)
                    }
                )
        assets = {
            str(item.get("symbol") or "").upper(): dict(item)
            for item in asset_page.raw_payload.get("assets", [])
            if isinstance(item, dict) and item.get("symbol")
        }
        return raw_object_ids, source_rows, snapshots, assets

    def _archive_page(
        self,
        page: MarketScreenerPage | StockSnapshotsPage | AssetCatalogPage,
    ) -> str:
        if isinstance(page, MarketScreenerPage):
            data_type = page.data_type
        elif isinstance(page, AssetCatalogPage):
            data_type = "active_us_equity_assets"
        else:
            data_type = "stock_snapshots"
        archived = self.archive.store_json(
            provider=page.provider,
            data_type=data_type,
            payload=page.raw_payload,
            request_metadata=page.request_metadata,
            provider_received_at=page.provider_received_at,
        )
        return self.market.register_raw_object(archived)

    @staticmethod
    def _source_rows(
        active_page: MarketScreenerPage,
        mover_page: MarketScreenerPage,
    ) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for rank, item in enumerate(
            active_page.raw_payload.get("most_actives") or (), 1
        ):
            symbol = str(item.get("symbol") or "").upper()
            if not symbol:
                continue
            row = rows.setdefault(symbol, {"source_tags": set()})
            row["active_rank"] = rank
            row["reported_volume"] = item.get("volume")
            row["reported_trade_count"] = item.get("trade_count")
            row["source_tags"].add("MOST_ACTIVE")
        for label in ("gainers", "losers"):
            for rank, item in enumerate(mover_page.raw_payload.get(label) or (), 1):
                symbol = str(item.get("symbol") or "").upper()
                if not symbol:
                    continue
                row = rows.setdefault(symbol, {"source_tags": set()})
                row["mover_rank"] = rank
                row["reported_price"] = item.get("price")
                row["reported_change"] = item.get("change")
                row["reported_percent_change"] = item.get("percent_change")
                row["source_tags"].add(
                    "TOP_GAINER" if label == "gainers" else "TOP_LOSER"
                )
        return rows

    def _rank_candidates(
        self,
        *,
        source_rows: dict[str, dict[str, Any]],
        snapshots: dict[str, dict[str, Any]],
        assets: dict[str, dict[str, Any]],
        as_of: datetime,
    ) -> list[MarketScanCandidate]:
        active_count = self.policy.integer("most_active_count")
        mover_count = self.policy.integer("mover_count")
        minimum_price = self.policy.decimal("minimum_price_usd")
        minimum_dollar_volume = self.policy.decimal("minimum_dollar_volume_usd")
        themes = self.policy.seed_themes
        benchmark_list = self.objects.get_list("benchmarks")
        benchmarks = {
            str(value).upper()
            for value in (benchmark_list["members"] if benchmark_list else [])
        }
        ranked: list[dict[str, Any]] = []
        for symbol, source in source_rows.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,23}", symbol):
                continue
            if symbol in benchmarks:
                continue
            if self.restrictions.is_restricted(symbol, as_of.date()):
                continue
            asset = assets.get(symbol)
            if asset is None:
                continue
            if (
                asset.get("class") != "us_equity"
                or asset.get("status") != "active"
                or asset.get("tradable") is not True
            ):
                continue
            asset_name = str(asset.get("name") or "").upper()
            if any(
                re.search(rf"\b{re.escape(term.upper())}\b", asset_name)
                for term in self.policy.excluded_asset_name_terms
            ):
                continue
            snapshot = snapshots.get(symbol)
            if snapshot is None:
                continue
            daily = snapshot.get("dailyBar") or {}
            previous = snapshot.get("prevDailyBar") or {}
            latest_trade = snapshot.get("latestTrade") or {}
            latest_quote = snapshot.get("latestQuote") or {}
            price = _decimal(
                latest_trade.get("p")
                or daily.get("c")
                or source.get("reported_price")
            )
            previous_close = _decimal(previous.get("c"))
            current_volume = _decimal(daily.get("v") or source.get("reported_volume") or 0)
            previous_volume = _decimal(previous.get("v") or 0)
            if price is None or price < minimum_price:
                continue
            current_dollar_volume = price * (current_volume or Decimal("0"))
            previous_dollar_volume = (previous_close or price) * (
                previous_volume or Decimal("0")
            )
            dollar_volume = max(current_dollar_volume, previous_dollar_volume)
            if dollar_volume < minimum_dollar_volume:
                continue
            reported_change = _decimal(source.get("reported_percent_change"))
            change_pct = reported_change
            if change_pct is None and previous_close and previous_close > 0:
                change_pct = (price / previous_close - Decimal("1")) * Decimal("100")
            change_pct = change_pct or Decimal("0")
            high = _decimal(daily.get("h"))
            low = _decimal(daily.get("l"))
            range_pct = (
                (high - low) / previous_close * Decimal("100")
                if high is not None and low is not None and previous_close
                else Decimal("0")
            )
            bid = _decimal(latest_quote.get("bp"))
            ask = _decimal(latest_quote.get("ap"))
            spread_bps = None
            if bid is not None and ask is not None and bid > 0 and ask >= bid:
                midpoint = (bid + ask) / Decimal("2")
                spread_bps = (ask - bid) / midpoint * Decimal("10000")
            score = min(abs(change_pct) * Decimal("2.5"), Decimal("25"))
            score += min(range_pct * Decimal("2"), Decimal("15"))
            liquidity_ratio = float(dollar_volume / minimum_dollar_volume)
            score += Decimal(str(round(min(math.log10(max(1.0, liquidity_ratio)) * 5, 10), 6)))
            reasons = []
            active_rank = source.get("active_rank")
            if active_rank is not None:
                contribution = Decimal("25") * Decimal(
                    str(max(0.0, 1 - (int(active_rank) - 1) / active_count))
                )
                score += contribution
                reasons.append(f"most-active rank {active_rank}")
            mover_rank = source.get("mover_rank")
            if mover_rank is not None:
                contribution = Decimal("20") * Decimal(
                    str(max(0.0, 1 - (int(mover_rank) - 1) / mover_count))
                )
                score += contribution
                reasons.append(f"market-mover rank {mover_rank}")
            theme = themes.get(symbol)
            if theme is not None:
                score += Decimal("8")
                reasons.append(f"theme seed: {theme}")
            if "FOCUS_WATCHLIST" in source["source_tags"]:
                score += Decimal("12")
                reasons.append("administrator focus watchlist")
            if theme is not None and Decimal("0.5") <= abs(change_pct) < Decimal("5"):
                score += Decimal("6")
                reasons.append("theme-aligned early movement")
            if abs(change_pct) >= Decimal("4") or mover_rank is not None:
                attention_class = "HOT"
            elif theme is not None and (
                abs(change_pct) >= Decimal("0.5") or active_rank is not None
            ):
                attention_class = "EMERGING"
            else:
                attention_class = "LIQUID_CORE"
            ranked.append(
                {
                    "symbol": symbol,
                    "score": score,
                    "attention_class": attention_class,
                    "metrics": {
                        "price_usd": str(price.quantize(Decimal("0.0001"))),
                        "day_change_pct": str(change_pct.quantize(Decimal("0.0001"))),
                        "intraday_range_pct": str(range_pct.quantize(Decimal("0.0001"))),
                        "dollar_volume_usd": str(dollar_volume.quantize(Decimal("1"))),
                        "volume": int(current_volume or 0),
                        "trade_count": int(
                            _decimal(daily.get("n") or source.get("reported_trade_count") or 0)
                            or 0
                        ),
                        "spread_bps": (
                            str(spread_bps.quantize(Decimal("0.01")))
                            if spread_bps is not None
                            else None
                        ),
                        "asset_name": str(asset.get("name") or ""),
                        "exchange": str(asset.get("exchange") or ""),
                    },
                    "source_tags": tuple(sorted(source["source_tags"])),
                    "theme": theme,
                    "reasons": tuple(reasons),
                }
            )
        ranked.sort(key=lambda item: (-item["score"], item["symbol"]))
        limit = self.policy.integer("deterministic_review_count")
        return [
            MarketScanCandidate(
                symbol=str(item["symbol"]),
                deterministic_rank=index + 1,
                final_rank=index + 1,
                deterministic_score=item["score"].quantize(Decimal("0.0001")),
                final_score=item["score"].quantize(Decimal("0.0001")),
                attention_class=item["attention_class"],
                selected=False,
                metrics=item["metrics"],
                source_tags=item["source_tags"],
                theme=item["theme"],
                deterministic_reasons=item["reasons"],
            )
            for index, item in enumerate(ranked[:limit])
        ]

    def _llm_interval_elapsed(self, now: datetime) -> bool:
        completed = next(
            (
                item
                for item in self.store.recent(limit=24)
                if item.get("llm_status") == "COMPLETED"
            ),
            None,
        )
        if completed is None:
            return True
        raw = completed.get("as_of")
        if not raw:
            return True
        previous = datetime.fromisoformat(str(raw)).astimezone(UTC)
        return now - previous >= timedelta(
            minutes=self.policy.integer("llm_min_interval_minutes")
        )

    async def _llm_rerank(
        self,
        candidates: list[MarketScanCandidate],
    ) -> tuple[list[MarketScanCandidate], str, str | None]:
        allowed = {item.symbol for item in candidates}
        previous = self.store.latest()
        previous_by_symbol = {
            str(item["symbol"]): item
            for item in (previous.get("candidates", []) if previous else [])
            if isinstance(item, dict) and item.get("symbol")
        }
        invocation = await self.llm_gateway.complete(
            LLMRequest(
                workload=LLMWorkload.ROUTINE_PIPELINE,
                prompt_version=MARKET_SCAN_PROMPT_VERSION,
                instructions=(
                    "You rank attention candidates for research, never for execution. Use "
                    "only the supplied market snapshot, source tags, theme labels, and "
                    "prior-scan context. Do not add symbols. Favor both current unusual "
                    "activity and plausible early theme follow-through, while naming gap, "
                    "liquidity, and crowded-"
                    "trade risks. Return strict JSON only: {\"picks\":[{\"symbol\":str,"
                    "\"priority_score\":integer 0..10,\"attention_class\":\"HOT\"|"
                    "\"EMERGING\"|\"LIQUID_CORE\",\"thesis\":str,\"risks\":[str]}]}."
                ),
                input_text=json.dumps(
                    {
                        "policy_version": self.policy.version,
                        "candidates": [
                            {
                                "symbol": item.symbol,
                                "deterministic_rank": item.deterministic_rank,
                                "deterministic_score": str(item.deterministic_score),
                                "attention_class": item.attention_class,
                                "metrics": item.metrics,
                                "source_tags": item.source_tags,
                                "theme": item.theme,
                                "previous_scan": (
                                    {
                                        "rank": previous_by_symbol[item.symbol].get(
                                            "final_rank"
                                        ),
                                        "attention_class": previous_by_symbol[
                                            item.symbol
                                        ].get("attention_class"),
                                        "day_change_pct": dict(
                                            previous_by_symbol[item.symbol].get(
                                                "metrics"
                                            )
                                            or {}
                                        ).get("day_change_pct"),
                                    }
                                    if item.symbol in previous_by_symbol
                                    else None
                                ),
                            }
                            for item in candidates
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                max_output_tokens=self.policy.integer("llm_max_output_tokens"),
                reasoning_effort="minimal",
                timeout_seconds=120,
            )
        )
        try:
            if invocation.status != LLMInvocationStatus.COMPLETED:
                raise ValueError("LLM scanner invocation did not complete")
            selection = _LLMSelection.model_validate(
                _json_object(invocation.output_text or "")
            )
            if any(item.symbol not in allowed for item in selection.picks):
                raise ValueError(
                    "LLM scanner introduced a symbol outside the deterministic set"
                )
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            return candidates, invocation.invocation_id, type(exc).__name__
        picks = {item.symbol: item for item in selection.picks}
        reranked = []
        for item in candidates:
            pick = picks.get(item.symbol)
            priority = pick.priority_score if pick is not None else 0
            reranked.append(
                item.model_copy(
                    update={
                        "final_score": (
                            item.deterministic_score + Decimal(priority) * Decimal("1.5")
                        ).quantize(Decimal("0.0001")),
                        "attention_class": (
                            pick.attention_class if pick is not None else item.attention_class
                        ),
                        "llm_priority_score": priority if pick is not None else None,
                        "llm_thesis": pick.thesis if pick is not None else None,
                        "llm_risks": pick.risks if pick is not None else (),
                    }
                )
            )
        reranked.sort(key=lambda item: (-item.final_score, item.symbol))
        return reranked, invocation.invocation_id, None

    def _record_failure(
        self,
        *,
        scan_id: str,
        as_of: datetime,
        error_code: str,
    ) -> dict[str, Any]:
        current = self.objects.get_list("candidate-list")
        fallback = list(current["members"]) if current else []
        if not fallback:
            universe = self.objects.get_list("trading-universe")
            fallback = list(universe["members"]) if universe else []
        payload = {
            "scan_id": scan_id,
            "cycle_key": as_of.strftime("%Y-%m-%dT%H"),
            "as_of": as_of.isoformat(),
            "status": "FAILED_FALLBACK",
            "policy_version": self.policy.version,
            "error_code": error_code,
            "source_symbol_count": 0,
            "eligible_count": 0,
            "selected_count": len(fallback),
            "selected_symbols": fallback,
            "raw_object_ids": [],
            "llm_status": "NOT_RUN",
            "llm_invocation_id": None,
            "llm_error_code": None,
            "candidates": [],
            "automatic_execution": False,
        }
        self.store.record(scan_id=scan_id, as_of=as_of, payload=payload)
        return payload
