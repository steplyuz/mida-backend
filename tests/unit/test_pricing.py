import pytest

from app.core.domain_errors import InvalidAmountError
from app.services.pricing_service import PricingService


@pytest.fixture
def pricing() -> PricingService:
    return PricingService(service_fee_bps=1000, max_amount_uzs=1_000_000_000)


def test_ten_percent_fee_on_one_million_uzs(pricing: PricingService) -> None:
    # 1 000 000 UZS = 100 000 000 tiyin
    price = pricing.calculate(100_000_000)
    assert price.service_fee_tiyin == 10_000_000  # 100 000 UZS
    assert price.total_amount_tiyin == 110_000_000  # 1 100 000 UZS
    assert price.currency == "UZS"
    assert price.service_fee_bps == 1000


@pytest.mark.parametrize(
    ("product_tiyin", "bps", "expected_fee"),
    [
        (1, 1000, 0),  # 0.1 -> 0
        (4, 1000, 0),  # 0.4 -> 0
        (5, 1000, 1),  # 0.5 -> 1 (half-up)
        (15, 1000, 2),  # 1.5 -> 2
        (25, 1000, 3),  # 2.5 -> 3
        (999, 0, 0),  # zero rate
        (999, 10_000, 999),  # 100 % rate
        (12_345, 333, 411),  # 411.2385 -> 411
    ],
)
def test_fee_rounds_half_up_with_integer_math(product_tiyin: int, bps: int, expected_fee: int) -> None:
    pricing = PricingService(service_fee_bps=bps, max_amount_uzs=1_000_000)
    assert pricing.calculate(product_tiyin).service_fee_tiyin == expected_fee


def test_total_always_equals_product_plus_fee(pricing: PricingService) -> None:
    for amount in (1, 77, 12_345, 99_999_999, 100_000_000):
        price = pricing.calculate(amount)
        assert price.total_amount_tiyin == price.product_amount_tiyin + price.service_fee_tiyin


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "100", None])
def test_non_positive_or_non_integer_amounts_are_rejected(pricing: PricingService, bad: object) -> None:
    with pytest.raises(InvalidAmountError):
        pricing.calculate(bad)  # type: ignore[arg-type]


def test_maximum_amount_is_inclusive_and_next_unit_is_rejected() -> None:
    pricing = PricingService(service_fee_bps=1000, max_amount_uzs=10)  # max 1000 tiyin
    assert pricing.calculate(1000).total_amount_tiyin == 1100
    with pytest.raises(InvalidAmountError):
        pricing.calculate(1001)


@pytest.mark.parametrize("bps", [-1, 10_001])
def test_constructor_rejects_invalid_rate(bps: int) -> None:
    with pytest.raises(ValueError):
        PricingService(service_fee_bps=bps, max_amount_uzs=1)


def test_constructor_rejects_non_positive_maximum() -> None:
    with pytest.raises(ValueError):
        PricingService(service_fee_bps=1000, max_amount_uzs=0)
