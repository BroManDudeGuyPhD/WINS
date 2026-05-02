"""
wins/shared/models.py
Pydantic models shared across services.
The DecisionOutput model mirrors the JSON schema in WINS.md §Decision Output Schema.
"""
from __future__ import annotations
from decimal import Decimal
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class Action(str, Enum):
    buy  = "buy"
    sell = "sell"
    hold = "hold"


class SignalType(str, Enum):
    catalyst  = "catalyst"
    sentiment = "sentiment"
    momentum  = "momentum"
    macro     = "macro"


class TimeHorizon(str, Enum):
    hours = "hours"
    days  = "days"
    week  = "week"


class MacroGate(str, Enum):
    pass_gate = "pass"
    block     = "block"


class RiskFlag(str, Enum):
    none    = "none"
    caution = "caution"
    high    = "high"


class DecisionOutput(BaseModel):
    """Structured output Claude must return every decision cycle."""
    action:             Action
    token:              str
    confidence:         Decimal = Field(ge=Decimal("0.0"), le=Decimal("1.0"))
    signal_type:        SignalType
    entry_price:        Decimal   = Field(ge=Decimal("0"))
    stop_loss_price:    Decimal   = Field(ge=Decimal("0"))
    target_price:       Decimal   = Field(ge=Decimal("0"))
    estimated_move_pct: int
    time_horizon:       TimeHorizon
    reasoning:          str
    macro_gate:         MacroGate
    risk_flag:          RiskFlag

    @field_validator("token")
    @classmethod
    def token_uppercase(cls, v: str) -> str:
        return v.upper()


class MarketSnapshot(BaseModel):
    """Price/volume data for a single token at a point in time."""
    token:          str
    price_usd:      Decimal
    volume_24h_usd: Decimal
    change_24h_pct: Decimal
    market_cap_usd: Optional[Decimal] = None
    btc_dominance:  Optional[Decimal] = None   # only populated for BTC row


class SignalBundle(BaseModel):
    """All pre-processed signals handed to the brain for one cycle."""
    token:             str
    market:            MarketSnapshot
    macro:             MarketSnapshot          # BTC snapshot
    news_summary:      str = ""
    social_summary:    str = ""
    social_raw:        dict = Field(default_factory=dict)  # raw LunarCrush fields for signal_log
    onchain_summary:   str = ""
    github_summary:    str = ""
