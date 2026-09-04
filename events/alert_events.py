"""Alert + Webhook system events for the OpenAlgo event bus."""

from events.base import Event


class AlertTriggeredEvent(Event):
    """An alert matched its condition and an event was recorded."""

    topic = "alert.triggered"

    def __init__(
        self,
        alert_id: str,
        user_id: str,
        symbol: str,
        event_type: str,
        signal: str | None,
        price: float | None,
        message: str | None,
    ):
        self.alert_id = alert_id
        self.user_id = user_id
        self.symbol = symbol
        self.event_type = event_type
        self.signal = signal
        self.price = price
        self.message = message
        super().__init__(topic=self.topic)


class AlertDeliveryEvent(Event):
    """A webhook delivery attempt finished (success or final failure)."""

    topic = "alert.delivery"

    def __init__(
        self,
        alert_id: str,
        event_id: str,
        status: str,
        http_status: int | None,
        error: str | None,
    ):
        self.alert_id = alert_id
        self.event_id = event_id
        self.status = status
        self.http_status = http_status
        self.error = error
        super().__init__(topic=self.topic)
