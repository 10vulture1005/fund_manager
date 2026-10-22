"""STRATEGY B — All-in-One Delta Trades, multi-strategy portfolio (prompt B, 2.2).

Exposes the strategy, the portfolio manager (allocation + global risk), and
the sleeve classes themselves as `Sleeves`.
"""
from strategy.all_in_one.allocation import Allocation, Allocator
from strategy.all_in_one.defaults import DEFAULTS, merge_defaults
from strategy.all_in_one.portfolio import PortfolioManager
from strategy.all_in_one.regime import RegimeEngine, RegimeSnapshot
from strategy.all_in_one.strategy import AllInOneStrategy
from strategy.all_in_one.sleeve_base import SleeveBase, SleeveStatus
from strategy.all_in_one.sleeve_btc_cp import BtcCpSleeve
from strategy.all_in_one.sleeve_eth_cc import EthCcSleeve
from strategy.all_in_one.sleeve_gold import GoldSleeve
from strategy.all_in_one.sleeve_stocks import StocksSleeve

Sleeves = {
    "ETH_CC": EthCcSleeve,
    "BTC_CP": BtcCpSleeve,
    "GOLD": GoldSleeve,
    "STOCKS": StocksSleeve,
}

__all__ = [
    "AllInOneStrategy",
    "PortfolioManager",
    "Sleeves",
    "SleeveBase",
    "SleeveStatus",
    "EthCcSleeve",
    "BtcCpSleeve",
    "GoldSleeve",
    "StocksSleeve",
    "Allocator",
    "Allocation",
    "RegimeEngine",
    "RegimeSnapshot",
    "DEFAULTS",
    "merge_defaults",
]
