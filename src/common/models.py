"""Data models for spread positions and trades."""

import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


class MarketRegime(str, Enum):
    RANGE = "range"
    UPTREND = "uptrend"
    DOWNTREND = "downtrend"
    UNCERTAIN = "uncertain"


class SpreadType(str, Enum):
    IRON_CONDOR = "iron_condor"
    PUT_CREDIT_SPREAD = "put_credit_spread"
    CALL_CREDIT_SPREAD = "call_credit_spread"


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED_PROFIT = "closed_profit"
    CLOSED_STOP = "closed_stop"
    CLOSED_EOD = "closed_eod"
    CLOSED_ROLL = "closed_roll"
    CLOSED_MANUAL = "closed_manual"


@dataclass
class SpreadLeg:
    leg_id: str
    contract_id: str
    side: str  # "sell" or "buy"
    option_type: str  # "put" or "call"
    strike: float
    quantity: int
    fill_price: float
    current_price: float = 0.0
    delta_at_entry: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SpreadLeg":
        return cls(**{k: v for k, v in data.items() if k in {f.name for f in fields(cls)}})


@dataclass
class SpreadPosition:
    spread_id: str
    spread_type: str  # SpreadType value
    underlying: str
    entry_time: str  # ISO format datetime
    expiration_date: str  # ISO format date
    strategy: str  # "IC_SPY" or "CS_SPY"

    # Legs stored as list of dicts for DynamoDB JSON
    legs: list[dict] = field(default_factory=list)

    # Aggregate metrics
    net_credit: float = 0.0
    current_value: float = 0.0
    unrealized_pnl: float = 0.0
    max_loss: float = 0.0

    # Risk levels
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0

    # Status
    status: str = PositionStatus.OPEN.value

    # Market context at entry
    market_regime: str = MarketRegime.UNCERTAIN.value
    vix_at_entry: float = 0.0
    spy_price_at_entry: float = 0.0
    expected_move_at_entry: float = 0.0

    def get_legs(self) -> list[SpreadLeg]:
        """Deserialize legs from dict list."""
        return [SpreadLeg.from_dict(leg) for leg in self.legs]

    def set_legs(self, leg_objects: list[SpreadLeg]) -> None:
        """Serialize legs to dict list for DynamoDB storage."""
        self.legs = [leg.to_dict() for leg in leg_objects]

    def to_dynamo_item(self) -> dict:
        """Serialize to DynamoDB item with Decimal conversion."""
        item = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if val is None:
                continue
            if f.name == "legs":
                item["legs"] = json.dumps(val)
            elif isinstance(val, float):
                item[f.name] = Decimal(str(round(val, 6)))
            elif isinstance(val, (datetime, date)):
                item[f.name] = val.isoformat()
            else:
                item[f.name] = str(val)
        return item

    @classmethod
    def from_dynamo_item(cls, item: dict) -> "SpreadPosition":
        """Deserialize from DynamoDB item."""
        kwargs = {}
        field_names = {f.name for f in fields(cls)}
        for f in fields(cls):
            if f.name not in item:
                continue
            val = item[f.name]
            if f.name == "legs":
                kwargs["legs"] = json.loads(val) if isinstance(val, str) else val
            elif isinstance(val, Decimal):
                kwargs[f.name] = float(val)
            else:
                kwargs[f.name] = val
        return cls(**kwargs)


@dataclass
class TradeRecord:
    trade_date: str  # ISO date
    spread_id: str
    strategy: str
    action: str  # "OPEN" or "CLOSE"
    spread_type: str
    underlying: str

    # Spread details
    net_credit: float = 0.0
    close_value: float = 0.0
    realized_pnl: float = 0.0

    # Leg details (JSON string)
    legs: str = ""

    # Market context
    market_regime: str = ""
    vix: float = 0.0
    spy_price: float = 0.0
    expected_move: float = 0.0

    # Close reason
    close_reason: str = ""  # "profit", "stop", "eod", "roll", "manual"

    def to_dynamo_item(self) -> dict:
        item = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if val is None or val == "":
                continue
            if isinstance(val, float):
                item[f.name] = Decimal(str(round(val, 6)))
            else:
                item[f.name] = str(val)
        return item


def generate_spread_id() -> str:
    """Generate a unique spread ID."""
    return str(uuid.uuid4())[:12]
