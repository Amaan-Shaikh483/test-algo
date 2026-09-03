"""Events for the Pine strategy engine, published on the existing event bus."""

from dataclasses import dataclass, field

from events.base import OrderEvent


@dataclass
class PineEvent(OrderEvent):
    """Base for Pine strategy events.

    mode: "paper" or "live" - which execution path the strategy instance uses.
    """

    mode: str = "paper"
    strategy_id: str = ""
    strategy_name: str = ""
    symbol: str = ""
    exchange: str = ""
    timeframe: str = ""


@dataclass
class PineSignalEvent(PineEvent):
    """Fired when the Pine runtime emits a strategy signal on a confirmed bar."""

    topic: str = "pine.signal"
    signal_id: str = ""
    signal: str = ""  # BUY | SELL
    kind: str = ""  # entry | close | exit_stop | exit_limit
    price: float = 0.0
    quantity: float = 0.0
    bar_time: float = 0.0
    bar_index: int = 0
    source: str = "realtime"


@dataclass
class PineAlertEvent(PineEvent):
    """Fired for alertcondition()/alert() firings on a confirmed bar."""

    topic: str = "pine.alert"
    alert_kind: str = ""  # condition | call
    title: str = ""
    message: str = ""
    bar_time: float = 0.0


@dataclass
class PineStatusEvent(PineEvent):
    """Fired on lifecycle transitions (RUNNING / PAUSED / STOPPED / ERROR)."""

    topic: str = "pine.status"
    status: str = ""
    detail: str = ""


@dataclass
class PineOrderEvent(PineEvent):
    """Fired after a signal was routed into the order pipeline."""

    topic: str = "pine.order"
    signal_id: str = ""
    signal: str = ""
    order_id: str = ""
    order_status: str = ""
    message: str = ""
    request_data: dict = field(default_factory=dict)
    response_data: dict = field(default_factory=dict)


@dataclass
class PineErrorEvent(PineEvent):
    """Fired when a running strategy hits a runtime error."""

    topic: str = "pine.error"
    error: str = ""
