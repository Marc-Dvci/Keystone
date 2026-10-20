"""Money in exact decimals. Airwallex amounts are major units (100 = one hundred dollars)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

MINOR_DIGITS = {"USD": 2, "EUR": 2, "GBP": 2, "CNY": 2, "JPY": 0, "SGD": 2, "AUD": 2, "HKD": 2}


def quantize(amount: Decimal | str | int | float, currency: str) -> Decimal:
    digits = MINOR_DIGITS.get(currency, 2)
    return Decimal(str(amount)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    @staticmethod
    def of(amount: Decimal | str | int | float, currency: str) -> "Money":
        return Money(quantize(amount, currency), currency)

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.amount - other.amount, self.currency)

    def __le__(self, other: "Money") -> bool:
        self._same(other)
        return self.amount <= other.amount

    def __lt__(self, other: "Money") -> bool:
        self._same(other)
        return self.amount < other.amount

    def _same(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch {self.currency} vs {other.currency}")

    def to_json(self) -> dict:
        return {"amount": str(self.amount), "currency": self.currency}

    @staticmethod
    def from_json(d: dict) -> "Money":
        return Money.of(d["amount"], d["currency"])

    def __str__(self) -> str:
        return f"{self.currency} {self.amount:,}"


def zero(currency: str) -> Money:
    return Money.of(0, currency)
