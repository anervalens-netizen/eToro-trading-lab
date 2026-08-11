from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from .mcp import EtoroMCPClient

DEMO_READ_SCOPE = "etoro-public:trade.demo:read"
DEMO_WRITE_SCOPE = "etoro-public:trade.demo:write"
DELEGATED_DEMO_SCOPES = (DEMO_READ_SCOPE, DEMO_WRITE_SCOPE)


@dataclass(frozen=True)
class AgentPortfolioTokenMetadata:
    token_id: str
    name: str
    scope_names: tuple[str, ...]
    expires_at: str | None


@dataclass(frozen=True)
class AgentPortfolioMetadata:
    portfolio_id: str
    name: str
    virtual_balance_usd: Decimal
    tokens: tuple[AgentPortfolioTokenMetadata, ...]


def _uuid(value: object, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid {field}") from exc


def parse_agent_portfolios(payload: Mapping[str, Any]) -> tuple[AgentPortfolioMetadata, ...]:
    rows = payload.get("agentPortfolios", [])
    if not isinstance(rows, list):
        raise ValueError("agentPortfolios must be a list")
    portfolios: list[AgentPortfolioMetadata] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid agent portfolio row")
        raw_tokens = row.get("userTokens", [])
        if not isinstance(raw_tokens, list):
            raise ValueError("agent portfolio tokens must be a list")
        tokens: list[AgentPortfolioTokenMetadata] = []
        for token in raw_tokens:
            if not isinstance(token, dict):
                raise ValueError("invalid agent portfolio token row")
            scope_names = token.get("scopeNames", [])
            if not isinstance(scope_names, list) or any(
                not isinstance(scope, str) for scope in scope_names
            ):
                raise ValueError("invalid agent portfolio token scopes")
            tokens.append(
                AgentPortfolioTokenMetadata(
                    token_id=_uuid(token.get("userTokenId"), "userTokenId"),
                    name=str(token.get("userTokenName", "")),
                    scope_names=tuple(sorted(set(scope_names))),
                    expires_at=(
                        None if token.get("expiresAt") is None else str(token["expiresAt"])
                    ),
                )
            )
        balance = Decimal(str(row.get("agentPortfolioVirtualBalance", "0")))
        if balance <= 0:
            raise ValueError("agent portfolio virtual balance must be positive")
        portfolios.append(
            AgentPortfolioMetadata(
                portfolio_id=_uuid(row.get("agentPortfolioId"), "agentPortfolioId"),
                name=str(row.get("agentPortfolioName", "")),
                virtual_balance_usd=balance,
                tokens=tuple(tokens),
            )
        )
    return tuple(portfolios)


def build_demo_agent_portfolio_request(
    *,
    investment_amount_usd: Decimal,
    portfolio_name: str,
    token_name: str,
    ipv4_whitelist: tuple[str, ...],
    expires_at: datetime,
    description: str = "Autonomous eToro DEMO trading research",
) -> dict[str, Any]:
    """Build, but never execute, the one-time money-moving provisioning request."""

    if investment_amount_usd <= 0:
        raise ValueError("investment amount must be positive")
    if investment_amount_usd != investment_amount_usd.quantize(Decimal("0.01")):
        raise ValueError("investment amount supports at most two decimal places")
    if not 6 <= len(portfolio_name) <= 10:
        raise ValueError("agent portfolio name must contain 6-10 characters")
    if not token_name.strip() or len(token_name) > 100:
        raise ValueError("agent token name is invalid")
    if not ipv4_whitelist:
        raise ValueError("an IPv4 whitelist is mandatory")
    normalized_ips = tuple(str(ipaddress.IPv4Address(value)) for value in ipv4_whitelist)
    if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
        raise ValueError("agent token expiry must be timezone-aware and in the future")
    return {
        "investmentAmountInUsd": float(investment_amount_usd),
        "agentPortfolioName": portfolio_name,
        "agentPortfolioDescription": description,
        "userTokenName": token_name,
        "scopeNames": list(DELEGATED_DEMO_SCOPES),
        "ipsWhitelist": list(normalized_ips),
        "expiresAt": expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


class AgentPortfolioReader:
    """Read-only management surface; provisioning writes are intentionally absent."""

    def __init__(self, client: EtoroMCPClient) -> None:
        self.client = client

    def list(self) -> tuple[AgentPortfolioMetadata, ...]:
        result = self.client.execute_read("/api/v1/agent-portfolios")
        if not result.is_success or not isinstance(result.body, dict):
            raise RuntimeError("failed to read eToro Agent Portfolios")
        return parse_agent_portfolios(result.body)

    def allowed_scopes(self) -> tuple[str, ...]:
        result = self.client.execute_read("/api/v2/agent-portfolios/user-tokens/scopes")
        if not result.is_success or not isinstance(result.body, dict):
            raise RuntimeError("failed to read Agent Portfolio token scopes")
        rows = result.body.get("scopes", [])
        if not isinstance(rows, list):
            raise ValueError("Agent Portfolio scopes response is invalid")
        return tuple(
            sorted(
                str(row["name"])
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("name"), str)
            )
        )
