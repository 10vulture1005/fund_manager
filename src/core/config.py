"""Config loading with [ASK]-parameter enforcement (both specs: 3.8).

Any config value tagged [ASK] in the prompt specs defaults to `null`.
Live trading is refused while any requested value is still null.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings

# Values that must be filled by the user before live trading is allowed.
ASK_KEYS: dict[str, list[str]] = {
    # rwa_positional (prompt A)
    "rwa_positional": [
        "recommended_margin",
        "min_turnover_usd",
        "leverage",
        "margin_mode",
    ],
    # all_in_one (prompt B)
    "all_in_one": [
        "eth_cc.lot_size_notional",
        "btc_cp.lot_size_notional",
        "gold.lot_size_notional",
        "stocks.lot_size_notional",
        "allocation.strategy_capital_usd",
        "allocation.base_weights.eth_cc",
        "allocation.base_weights.btc_cp",
        "allocation.base_weights.gold",
        "allocation.base_weights.stocks",
        "stocks.stock_symbols",
    ],
}


class ApiConfig(BaseModel):
    env: str = "testnet"  # testnet | prod
    api_key: str = ""
    api_secret: str = ""
    prod_rest: str = "https://api.india.delta.exchange"
    testnet_rest: str = "https://cdn-ind.testnet.deltaex.org"
    prod_ws: str = "wss://socket.india.delta.exchange"
    testnet_ws: str = "wss://socket.ind.testnet.deltaex.org"

    @property
    def rest_base(self) -> str:
        return self.prod_rest if self.env == "prod" else self.testnet_rest

    @property
    def ws_base(self) -> str:
        return self.prod_ws if self.env == "prod" else self.testnet_ws


class LlmSecrets(BaseModel):
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None


class Config(BaseModel):
    env: str = "testnet"
    mode: str = "paper"  # backtest | paper | live
    api: ApiConfig = Field(default_factory=ApiConfig)
    live_prod_required_flag: bool = Field(
        default=False, exclude=True, repr=False
    )  # set from CLI --i-understand-this-is-live
    state_dir: str = "state"
    data_dir: str = "data"
    log_dir: str = "logs"
    flags: dict[str, Any] = Field(default_factory=dict)

    atmosphere: LlmSecrets = Field(default_factory=LlmSecrets)

    rwa_positional: dict[str, Any] = Field(default_factory=dict)
    all_in_one: dict[str, Any] = Field(default_factory=dict)
    backtest: dict[str, Any] = Field(default_factory=dict)

    def missing_ask_keys(self, strategy: str) -> list[str]:
        """Return the dotted paths of [ASK] keys still null for a strategy."""
        missing: list[str] = []
        section = getattr(self, strategy, {}) or {}
        for dotted in ASK_KEYS.get(strategy, []):
            node: Any = section
            ok = True
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    ok = False
                    break
                node = node[part]
            if not ok or node is None:
                missing.append(f"{strategy}.{dotted}")
        return missing

    def assert_live_ready(self, strategy: str) -> None:
        missing = self.missing_ask_keys(strategy)
        if missing:
            raise ValueError(
                "Cannot run live with these [ASK] values still null in config.yaml: "
                + ", ".join(missing)
            )
        if self.env == "prod" and not self.flags.get("i_understand_this_is_live"):
            raise ValueError(
                "prod requires the CLI flag --i-understand-this-is-live (spec 1.1)"
            )


def load_config(path: str = "config.yaml", overrides: dict[str, Any] | None = None) -> Config:
    data: dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
    # secrets via env
    api = data.setdefault("api", {})
    api["api_key"] = os.environ.get("DELTA_API_KEY", api.get("api_key", ""))
    api["api_secret"] = os.environ.get("DELTA_API_SECRET", api.get("api_secret", ""))
    if overrides:
        for k, v in overrides.items():
            data[k] = v
    return Config.model_validate(data)
