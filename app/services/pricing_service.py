"""Single place where commission is calculated. All amounts are integer tiyin."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.core.domain_errors import InvalidAmountError

TIYIN_PER_UZS = 100
BPS_DENOMINATOR = 10_000
CURRENCY = "UZS"


@dataclass(frozen=True, slots=True)
class PriceBreakdown:
    product_amount_tiyin: int
    service_fee_bps: int
    service_fee_tiyin: int
    total_amount_tiyin: int
    currency: str = CURRENCY


class PricingService:
    def __init__(self, *, service_fee_bps: int, max_amount_uzs: int) -> None:
        if not 0 <= service_fee_bps <= BPS_DENOMINATOR:
            raise ValueError("service_fee_bps must be within 0..10000")
        if max_amount_uzs <= 0:
            raise ValueError("max_amount_uzs must be positive")
        self._fee_bps = service_fee_bps
        self._max_amount_tiyin = max_amount_uzs * TIYIN_PER_UZS

    @classmethod
    def from_settings(cls, settings: Settings) -> PricingService:
        return cls(
            service_fee_bps=settings.service_fee_bps,
            max_amount_uzs=settings.max_deal_amount_uzs,
        )

    @property
    def fee_bps(self) -> int:
        return self._fee_bps

    def calculate(self, product_amount_tiyin: int) -> PriceBreakdown:
        """Fee = product * bps / 10000, rounded half-up to the nearest tiyin (integer math only)."""
        if isinstance(product_amount_tiyin, bool) or not isinstance(product_amount_tiyin, int):
            raise InvalidAmountError("Amount must be an integer number of tiyin.")
        if product_amount_tiyin <= 0:
            raise InvalidAmountError("Amount must be greater than zero.")
        if product_amount_tiyin > self._max_amount_tiyin:
            raise InvalidAmountError("Amount exceeds the maximum allowed for a single deal.")

        fee = (product_amount_tiyin * self._fee_bps + BPS_DENOMINATOR // 2) // BPS_DENOMINATOR
        return PriceBreakdown(
            product_amount_tiyin=product_amount_tiyin,
            service_fee_bps=self._fee_bps,
            service_fee_tiyin=fee,
            total_amount_tiyin=product_amount_tiyin + fee,
        )
