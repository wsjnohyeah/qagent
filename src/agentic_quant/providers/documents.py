from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Self
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from agentic_quant.domain import CorporateFact, SourceDocument, SourceTier
from agentic_quant.ids import uuid7
from agentic_quant.providers.base import (
    CompanyTickerMapPage,
    CorporateFactsPage,
    CorporateFactsRequest,
    DocumentFetchRequest,
    DocumentPage,
)


class DocumentProviderConfigurationError(RuntimeError):
    pass


class DocumentProviderResponseError(RuntimeError):
    def __init__(self, *, provider: str, status_code: int, endpoint: str) -> None:
        super().__init__(f"{provider} request failed ({status_code}) at {endpoint}")
        self.provider = provider
        self.status_code = status_code
        self.endpoint = endpoint


class FeatureDisabledError(RuntimeError):
    pass


def _utc_timestamp(value: str | None, *, fallback: datetime | None = None) -> datetime:
    if not value:
        if fallback is None:
            raise ValueError("A timestamp is required")
        return fallback.astimezone(UTC)
    normalized = value.strip()
    if re.fullmatch(r"\d{14}", normalized):
        return datetime.strptime(normalized, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        return datetime.fromisoformat(normalized).replace(tzinfo=UTC)
    return datetime.fromisoformat(normalized.replace("Z", "+00:00")).astimezone(UTC)


def _feed_timestamp(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return _utc_timestamp(value, fallback=fallback)


def _plain_text(value: str | None) -> str | None:
    if not value:
        return None
    without_tags = re.sub(r"<[^>]+>", " ", value)
    normalized = " ".join(html.unescape(without_tags).split())
    return normalized or None


class _HttpJsonProvider:
    name: str

    def __init__(
        self,
        *,
        base_url: str,
        headers: dict[str, str],
        client: httpx.AsyncClient | None,
        max_retries: int = 3,
    ) -> None:
        self._headers = headers
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(
                    endpoint,
                    params=params,
                    headers=self._headers,
                )
            except httpx.RequestError:
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise
            if (response.status_code == 429 or response.status_code >= 500) and (
                attempt < self._max_retries
            ):
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            if response.is_error:
                raise DocumentProviderResponseError(
                    provider=self.name,
                    status_code=response.status_code,
                    endpoint=endpoint,
                )
            return response
        raise RuntimeError("unreachable provider retry state")


class AlpacaNewsProvider(_HttpJsonProvider):
    name = "alpaca_news"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = "https://data.alpaca.markets",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key or not api_secret:
            raise DocumentProviderConfigurationError("Alpaca credentials are required")
        super().__init__(
            base_url=base_url,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": api_secret,
            },
            client=client,
        )
        self._submission_files: tuple[str, ...] = ()
        self._submission_context: dict[str, str] = {}

    async def fetch_documents_page(
        self,
        request: DocumentFetchRequest,
        *,
        page_token: str | None = None,
    ) -> DocumentPage:
        endpoint = "/v1beta1/news"
        params: dict[str, Any] = {
            "symbols": ",".join(symbol.upper() for symbol in request.symbols),
            "limit": request.limit,
            "sort": "asc",
            "include_content": "true",
        }
        if request.start:
            params["start"] = request.start.astimezone(UTC).isoformat()
        if request.end:
            params["end"] = request.end.astimezone(UTC).isoformat()
        if page_token:
            params["page_token"] = page_token
        response = await self._get(endpoint, params)
        received_at = datetime.now(UTC)
        payload = response.json()
        documents = tuple(
            self._normalize_article(item, received_at, request.symbols)
            for item in payload.get("news", ())
        )
        return DocumentPage(
            provider=self.name,
            data_type="news",
            provider_received_at=received_at,
            request_metadata={"endpoint": endpoint, "params": params},
            raw_payload=payload,
            documents=documents,
            next_page_token=payload.get("next_page_token"),
        )

    def _normalize_article(
        self,
        item: dict[str, Any],
        received_at: datetime,
        requested_symbols: tuple[str, ...],
    ) -> SourceDocument:
        published_at = _utc_timestamp(item.get("created_at"), fallback=received_at)
        updated_at = _utc_timestamp(item.get("updated_at"), fallback=published_at)
        article_id = str(item["id"])
        provider_symbols = {str(value).upper() for value in item.get("symbols", ())}
        requested_order = [
            symbol.upper() for symbol in requested_symbols if symbol.upper() in provider_symbols
        ]
        remaining = sorted(provider_symbols - set(requested_order))
        return SourceDocument(
            document_id=uuid7(),
            provider_document_id=article_id,
            provider=self.name,
            canonical_url=str(item.get("url") or f"urn:alpaca-news:{article_id}"),
            source_kind="news",
            source_tier=SourceTier.SECONDARY,
            publisher=str(item.get("source") or "Alpaca News"),
            title=str(item.get("headline") or "Untitled news item"),
            summary=_plain_text(item.get("summary")),
            body_text=_plain_text(item.get("content")),
            symbols=tuple(requested_order + remaining),
            published_at=published_at,
            updated_at=updated_at,
            ingested_at=received_at,
            raw_object_id="PENDING_ARCHIVE",
        )


class SecEdgarProvider(_HttpJsonProvider):
    name = "sec_edgar"

    def __init__(
        self,
        *,
        user_agent: str,
        base_url: str = "https://data.sec.gov",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise DocumentProviderConfigurationError(
                "SEC_USER_AGENT must identify an operator and contact email"
            )
        super().__init__(
            base_url=base_url,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            client=client,
        )

    async def fetch_company_ticker_map(self) -> CompanyTickerMapPage:
        endpoint = "https://www.sec.gov/files/company_tickers.json"
        response = await self._get(endpoint)
        received_at = datetime.now(UTC)
        payload = response.json()
        cik_by_symbol = {
            str(item["ticker"]).upper(): str(item["cik_str"]).zfill(10)
            for item in payload.values()
            if item.get("ticker") and item.get("cik_str") is not None
        }
        return CompanyTickerMapPage(
            provider=self.name,
            data_type="company_tickers",
            provider_received_at=received_at,
            request_metadata={"endpoint": endpoint},
            raw_payload=payload,
            cik_by_symbol=cik_by_symbol,
        )

    async def fetch_documents_page(
        self,
        request: DocumentFetchRequest,
        *,
        page_token: str | None = None,
    ) -> DocumentPage:
        if not request.cik:
            raise ValueError("SEC document requests require a CIK")
        cik = request.cik.zfill(10)
        endpoint = (
            f"/submissions/{page_token}"
            if page_token is not None
            else f"/submissions/CIK{cik}.json"
        )
        response = await self._get(endpoint)
        received_at = datetime.now(UTC)
        payload = response.json()
        if page_token is None:
            self._submission_context = {
                "name": str(payload.get("name") or request.symbols[0].upper()),
                "cik": str(payload.get("cik") or cik).zfill(10),
            }
            self._submission_files = tuple(
                str(item["name"])
                for item in payload.get("filings", {}).get("files", ())
                if item.get("name")
                and self._submission_file_overlaps(item, request)
            )
        documents = self._normalize_filings(payload, request, received_at)
        next_page_token: str | None = None
        if page_token is None and self._submission_files:
            next_page_token = self._submission_files[0]
        elif page_token in self._submission_files:
            index = self._submission_files.index(page_token)
            if index + 1 < len(self._submission_files):
                next_page_token = self._submission_files[index + 1]
        return DocumentPage(
            provider=self.name,
            data_type="sec_filings",
            provider_received_at=received_at,
            request_metadata={
                "endpoint": endpoint,
                "symbol": request.symbols[0].upper(),
                "forms": request.forms,
            },
            raw_payload=payload,
            documents=documents,
            next_page_token=next_page_token,
        )

    @staticmethod
    def _submission_file_overlaps(
        item: dict[str, Any],
        request: DocumentFetchRequest,
    ) -> bool:
        file_start = (
            _utc_timestamp(str(item["filingFrom"]))
            if item.get("filingFrom")
            else None
        )
        file_end = (
            _utc_timestamp(str(item["filingTo"]))
            if item.get("filingTo")
            else None
        )
        if request.start is not None and file_end is not None:
            if file_end < request.start.astimezone(UTC):
                return False
        if request.end is not None and file_start is not None:
            if file_start > request.end.astimezone(UTC):
                return False
        return True

    def _normalize_filings(
        self,
        payload: dict[str, Any],
        request: DocumentFetchRequest,
        received_at: datetime,
    ) -> tuple[SourceDocument, ...]:
        recent = payload.get("filings", {}).get("recent") or payload
        accessions = recent.get("accessionNumber", ())
        allowed_forms = {value.upper() for value in request.forms}
        issuer_name = str(
            payload.get("name")
            or self._submission_context.get("name")
            or request.symbols[0].upper()
        )
        cik = str(
            payload.get("cik")
            or self._submission_context.get("cik")
            or request.cik
        ).zfill(10)
        cik_path = str(int(cik))
        documents: list[SourceDocument] = []
        for index, accession in enumerate(accessions):
            form = self._at(recent, "form", index)
            primary_document = self._at(recent, "primaryDocument", index)
            if not form or not primary_document:
                continue
            if allowed_forms and form.upper() not in allowed_forms:
                continue
            accession_compact = str(accession).replace("-", "")
            published_at = _utc_timestamp(
                self._at(recent, "acceptanceDateTime", index)
                or self._at(recent, "filingDate", index),
                fallback=received_at,
            )
            if request.start and published_at < request.start.astimezone(UTC):
                continue
            if request.end and published_at > request.end.astimezone(UTC):
                continue
            description = self._at(recent, "primaryDocDescription", index)
            report_date = self._at(recent, "reportDate", index)
            documents.append(
                SourceDocument(
                    document_id=uuid7(),
                    provider_document_id=str(accession),
                    provider=self.name,
                    canonical_url=(
                        "https://www.sec.gov/Archives/edgar/data/"
                        f"{cik_path}/{accession_compact}/{primary_document}"
                    ),
                    source_kind="sec_filing",
                    source_tier=SourceTier.PRIMARY,
                    publisher="U.S. Securities and Exchange Commission",
                    title=f"{issuer_name} {form} filing",
                    summary="; ".join(
                        value
                        for value in (
                            description,
                            f"report date {report_date}" if report_date else None,
                        )
                        if value
                    )
                    or None,
                    symbols=(request.symbols[0].upper(),),
                    issuer_name=issuer_name,
                    cik=cik,
                    published_at=published_at,
                    ingested_at=received_at,
                    raw_object_id="PENDING_ARCHIVE",
                )
            )
            if len(documents) >= request.limit:
                break
        return tuple(documents)

    async def fetch_company_facts(
        self,
        request: CorporateFactsRequest,
    ) -> CorporateFactsPage:
        cik = request.cik.zfill(10)
        endpoint = f"/api/xbrl/companyfacts/CIK{cik}.json"
        response = await self._get(endpoint)
        received_at = datetime.now(UTC)
        payload = response.json()
        facts = self._normalize_company_facts(payload, request, received_at)
        return CorporateFactsPage(
            provider=self.name,
            data_type="company_facts",
            provider_received_at=received_at,
            request_metadata={
                "endpoint": endpoint,
                "symbol": request.symbol.upper(),
                "taxonomies": request.taxonomies,
                "max_facts": request.max_facts,
                "start": request.start.isoformat() if request.start else None,
                "end": request.end.isoformat() if request.end else None,
            },
            raw_payload=payload,
            facts=facts,
        )

    def _normalize_company_facts(
        self,
        payload: dict[str, Any],
        request: CorporateFactsRequest,
        received_at: datetime,
    ) -> tuple[CorporateFact, ...]:
        issuer_name = str(payload.get("entityName") or request.symbol.upper())
        cik = str(payload.get("cik") or request.cik).zfill(10)
        records: list[CorporateFact] = []
        namespaces = payload.get("facts", {})
        for taxonomy in request.taxonomies:
            for tag, definition in namespaces.get(taxonomy, {}).items():
                for unit, observations in definition.get("units", {}).items():
                    for item in observations:
                        end = item.get("end")
                        filed = item.get("filed")
                        form = item.get("form")
                        if not end or not filed or not form:
                            continue
                        period_end = _utc_timestamp(str(end))
                        if request.start and period_end < request.start.astimezone(UTC):
                            continue
                        if request.end and period_end > request.end.astimezone(UTC):
                            continue
                        value_text = str(item.get("val"))
                        fingerprint_material = json.dumps(
                            {
                                "cik": cik,
                                "taxonomy": taxonomy,
                                "tag": tag,
                                "unit": unit,
                                "start": item.get("start"),
                                "end": end,
                                "filed": filed,
                                "form": form,
                                "accession": item.get("accn"),
                                "value": value_text,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        accepted_at = (
                            _utc_timestamp(item["accepted"])
                            if item.get("accepted")
                            else None
                        )
                        filed_at = _utc_timestamp(str(filed))
                        # SEC company-facts often provides only a calendar date in
                        # ``filed``. Midnight UTC is not evidence that the fact was
                        # publicly available then. Without an acceptance timestamp the
                        # first defensible availability is this ingestion receipt.
                        filed_has_intraday_precision = not bool(
                            re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(filed).strip())
                        )
                        available_from = (
                            accepted_at
                            or (filed_at if filed_has_intraday_precision else received_at)
                        )
                        try:
                            numeric_value = Decimal(value_text)
                        except InvalidOperation:
                            numeric_value = None
                        records.append(
                            CorporateFact(
                                fact_id=uuid7(),
                                fact_fingerprint=hashlib.sha256(
                                    fingerprint_material.encode()
                                ).hexdigest(),
                                symbol=request.symbol.upper(),
                                cik=cik,
                                issuer_name=issuer_name,
                                taxonomy=taxonomy,
                                tag=str(tag),
                                unit=str(unit),
                                period_start=(
                                    _utc_timestamp(item["start"])
                                    if item.get("start")
                                    else None
                                ),
                                period_end=period_end,
                                filed_at=filed_at,
                                accepted_at=accepted_at,
                                fiscal_year=(int(item["fy"]) if item.get("fy") else None),
                                fiscal_period=item.get("fp"),
                                form=str(form),
                                accession_number=item.get("accn"),
                                numeric_value=numeric_value,
                                value_text=value_text,
                                available_from=available_from,
                                raw_object_id="PENDING_ARCHIVE",
                                ingested_at=received_at,
                            )
                        )
        records.sort(key=lambda fact: fact.available_from, reverse=True)
        return tuple(records[: request.max_facts])

    @staticmethod
    def _at(values: dict[str, Any], key: str, index: int) -> str | None:
        items = values.get(key, ())
        if index >= len(items):
            return None
        value = items[index]
        return str(value) if value else None


class InvestorRelationsFeedProvider(_HttpJsonProvider):
    name = "investor_relations"

    def __init__(
        self,
        *,
        feed_url: str,
        expected_hostname: str,
        issuer_name: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlparse(feed_url)
        if parsed.scheme != "https" or parsed.hostname != expected_hostname:
            raise DocumentProviderConfigurationError(
                "IR feed must use HTTPS and match its explicitly approved hostname"
            )
        self.feed_url = feed_url
        self.expected_hostname = expected_hostname
        self.issuer_name = issuer_name
        super().__init__(base_url=feed_url, headers={}, client=client)

    async def fetch_documents_page(
        self,
        request: DocumentFetchRequest,
        *,
        page_token: str | None = None,
    ) -> DocumentPage:
        if page_token is not None:
            raise ValueError("IR feeds do not support page tokens")
        response = await self._get(self.feed_url)
        if response.url.host != self.expected_hostname:
            raise DocumentProviderConfigurationError(
                "IR feed redirected outside its explicitly approved hostname"
            )
        if len(response.content) > 5_000_000:
            raise ValueError("IR feed exceeds the 5 MB safety limit")
        received_at = datetime.now(UTC)
        documents = self._parse_feed(response.text, request, received_at)
        return DocumentPage(
            provider=self.name,
            data_type="ir_releases",
            provider_received_at=received_at,
            request_metadata={"feed_url": self.feed_url},
            raw_payload={"feed_url": self.feed_url, "xml": response.text},
            documents=documents,
        )

    def _parse_feed(
        self,
        xml_text: str,
        request: DocumentFetchRequest,
        received_at: datetime,
    ) -> tuple[SourceDocument, ...]:
        root = ElementTree.fromstring(xml_text)
        entries = root.findall(".//item") or root.findall(".//{*}entry")
        documents: list[SourceDocument] = []
        for entry in entries[: request.limit]:
            title = self._child_text(entry, "title") or "Untitled IR release"
            link = self._child_text(entry, "link") or self._link_href(entry)
            provider_id = (
                self._child_text(entry, "guid")
                or self._child_text(entry, "id")
                or link
                or hashlib.sha256(title.encode()).hexdigest()
            )
            published = _feed_timestamp(
                self._child_text(entry, "pubDate")
                or self._child_text(entry, "published")
                or self._child_text(entry, "updated"),
                received_at,
            )
            updated_value = self._child_text(entry, "updated")
            documents.append(
                SourceDocument(
                    document_id=uuid7(),
                    provider_document_id=provider_id,
                    provider=self.name,
                    canonical_url=link or f"urn:ir-feed:{provider_id}",
                    source_kind="ir_release",
                    source_tier=SourceTier.PRIMARY,
                    publisher=self.issuer_name,
                    title=title,
                    summary=_plain_text(
                        self._child_text(entry, "description")
                        or self._child_text(entry, "summary")
                    ),
                    body_text=_plain_text(self._child_text(entry, "content")),
                    symbols=tuple(symbol.upper() for symbol in request.symbols),
                    issuer_name=self.issuer_name,
                    published_at=published,
                    updated_at=(
                        _feed_timestamp(updated_value, published)
                        if updated_value
                        else None
                    ),
                    ingested_at=received_at,
                    raw_object_id="PENDING_ARCHIVE",
                )
            )
        return tuple(documents)

    @staticmethod
    def _child_text(entry: ElementTree.Element, local_name: str) -> str | None:
        child = entry.find(local_name)
        if child is None:
            child = entry.find(f"{{*}}{local_name}")
        if child is None or child.text is None:
            return None
        return child.text.strip()

    @staticmethod
    def _link_href(entry: ElementTree.Element) -> str | None:
        child = entry.find("{*}link")
        return child.attrib.get("href") if child is not None else None


class SocialAggregateProvider(_HttpJsonProvider):
    name = "social_aggregate"

    def __init__(
        self,
        *,
        enabled: bool,
        endpoint: str,
        token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not enabled:
            raise FeatureDisabledError("Social aggregate ingestion is disabled")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname:
            raise DocumentProviderConfigurationError(
                "Social aggregate endpoint must be an HTTPS URL"
            )
        if not token:
            raise DocumentProviderConfigurationError("Social aggregate token is required")
        self.endpoint = endpoint
        super().__init__(
            base_url=endpoint,
            headers={"Authorization": f"Bearer {token}"},
            client=client,
        )

    async def fetch_documents_page(
        self,
        request: DocumentFetchRequest,
        *,
        page_token: str | None = None,
    ) -> DocumentPage:
        params: dict[str, Any] = {
            "symbols": ",".join(request.symbols),
            "limit": request.limit,
        }
        if page_token:
            params["page_token"] = page_token
        response = await self._get(self.endpoint, params)
        received_at = datetime.now(UTC)
        payload = response.json()
        documents = tuple(
            SourceDocument(
                document_id=uuid7(),
                provider_document_id=str(item["id"]),
                provider=self.name,
                canonical_url=str(item.get("url") or f"urn:social-aggregate:{item['id']}"),
                source_kind="social_aggregate",
                source_tier=SourceTier.AGGREGATE,
                publisher=str(item.get("provider") or "Configured social aggregate"),
                title=str(item.get("title") or "Social activity aggregate"),
                summary=_plain_text(item.get("summary")),
                symbols=tuple(str(value).upper() for value in item.get("symbols", ())),
                published_at=_utc_timestamp(item.get("published_at"), fallback=received_at),
                ingested_at=received_at,
                raw_object_id="PENDING_ARCHIVE",
            )
            for item in payload.get("items", ())
        )
        return DocumentPage(
            provider=self.name,
            data_type="social_aggregates",
            provider_received_at=received_at,
            request_metadata={"endpoint": self.endpoint, "symbols": request.symbols},
            raw_payload=payload,
            documents=documents,
            next_page_token=payload.get("next_page_token"),
        )
