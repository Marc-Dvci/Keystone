"""Shapes and the interface Keystone needs from Airwallex. Two implementations sit behind it:
``client.AirwallexClient`` calls the sandbox REST API, ``fake.FakeAirwallex`` mirrors the documented
behaviour in memory for tests and the no-credentials replay mode."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable, Protocol


class AirwallexError(Exception):
    def __init__(self, code: str, message: str = "", status: int = 400, raw: Any = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.raw = raw


# Payment intent statuses, as Airwallex names them.
REQUIRES_PAYMENT_METHOD = "REQUIRES_PAYMENT_METHOD"
REQUIRES_CAPTURE = "REQUIRES_CAPTURE"
SUCCEEDED = "SUCCEEDED"
CANCELLED = "CANCELLED"


@dataclass
class Intent:
    id: str
    amount: Decimal
    currency: str
    status: str
    merchant_order_id: str
    captured_amount: Decimal = Decimal(0)
    metadata: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class CardDetails:
    number: str
    expiry_month: str
    expiry_year: str
    cvc: str
    name: str = "Keystone Agent"


@dataclass
class Controls:
    per_transaction_limit: Decimal
    currency: str
    allowed_mccs: list[int]
    active_from: datetime | None = None
    active_to: datetime | None = None
    all_time_limit: Decimal | None = None


@dataclass
class Card:
    id: str
    status: str
    controls: Controls
    nickname: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class CardTxn:
    id: str
    card_id: str
    transaction_type: str  # AUTHORIZATION | CLEARING | REVERSAL | REFUND
    status: str  # PENDING | APPROVED | FAILED
    amount: Decimal
    currency: str
    mcc: int
    merchant_name: str
    failure_reason: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Quote:
    quote_id: str
    sell_currency: str
    buy_currency: str
    buy_amount: Decimal
    sell_amount: Decimal
    client_rate: Decimal
    valid_to: datetime
    raw: dict = field(default_factory=dict)


@dataclass
class Conversion:
    id: str
    quote_id: str | None
    sell_currency: str
    buy_currency: str
    sell_amount: Decimal
    buy_amount: Decimal
    client_rate: Decimal
    status: str
    raw: dict = field(default_factory=dict)


RemoteAuth = Callable[[dict], Awaitable[dict]]


class Acquiring(Protocol):
    async def create_intent(self, amount: Decimal, currency: str, merchant_order_id: str, request_id: str,
                            metadata: dict) -> Intent: ...

    async def confirm_hold(self, intent_id: str, card: CardDetails, request_id: str, pre_auth: bool) -> Intent: ...

    async def capture(self, intent_id: str, amount: Decimal, request_id: str) -> Intent: ...

    async def cancel(self, intent_id: str, reason: str, request_id: str) -> Intent: ...

    async def get_intent(self, intent_id: str) -> Intent: ...


class Issuing(Protocol):
    async def ensure_cardholder(self) -> str: ...

    async def create_card(self, cardholder_id: str, controls: Controls, nickname: str, request_id: str) -> Card: ...

    async def card_details(self, card_id: str) -> CardDetails: ...

    async def set_card_status(self, card_id: str, status: str) -> Card: ...

    async def simulate_authorization(self, card_id: str, card_number: str, amount: Decimal, currency: str,
                                     mcc: int, merchant_name: str) -> CardTxn: ...

    async def simulate_capture(self, txn_id: str, amount: Decimal) -> CardTxn: ...

    async def simulate_reverse(self, txn_id: str, amount: Decimal) -> CardTxn: ...

    async def card_transactions(self, card_id: str) -> list[CardTxn]: ...


class FX(Protocol):
    async def quote(self, sell_currency: str, buy_currency: str, buy_amount: Decimal, validity: str) -> Quote: ...

    async def convert(self, quote: Quote, request_id: str) -> Conversion: ...

    async def spot(self, sell_currency: str, buy_currency: str) -> Decimal: ...

    async def balances(self) -> dict[str, Decimal]: ...


class Airwallex(Acquiring, Issuing, FX, Protocol):
    mode: str
