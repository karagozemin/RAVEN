from raven.pricing.fair_value import FairValue
from raven.quoting.engine import QuoteEngine
from raven.quoting.inventory import Inventory


FAIR = FairValue(
    market="match_winner",
    probabilities={"home": 0.5},
    market_probs={"home": 0.5},
    model_probs=None,
    model_weight=0.0,
    cap_binding=False,
)


def test_long_limit_removes_only_risk_increasing_bid() -> None:
    inventory = Inventory()
    inventory.apply_fill("match_winner", "home", 150.0, 0.5)
    quote = QuoteEngine(max_position=150.0).quote(FAIR, inventory).outcome("home")
    assert quote is not None
    assert quote.bid_size == 0.0
    assert quote.ask_size > 0.0


def test_short_limit_removes_only_risk_increasing_ask() -> None:
    inventory = Inventory()
    inventory.apply_fill("match_winner", "home", -150.0, 0.5)
    quote = QuoteEngine(max_position=150.0).quote(FAIR, inventory).outcome("home")
    assert quote is not None
    assert quote.bid_size > 0.0
    assert quote.ask_size == 0.0
