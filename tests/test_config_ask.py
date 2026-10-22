import sys
import pytest

sys.path.insert(0, "src")

from core.config import load_config, Config


def test_load_config_and_ask_enforcement(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        """
env: testnet
mode: live
rwa_positional:
  margin_mode: null
  leverage: null
  recommended_margin: null
  min_turnover_usd: null
all_in_one:
  eth_cc:
    lot_size_notional: null
  allocation:
    base_weights:
      eth_cc: null
  stocks:
    stock_symbols: null
"""
    )
    cfg = load_config(str(p))
    assert cfg.env == "testnet"
    missing = cfg.missing_ask_keys("rwa_positional")
    assert "rwa_positional.margin_mode" in missing and "rwa_positional.leverage" in missing
    with pytest.raises(ValueError):
        cfg.assert_live_ready("rwa_positional")
    # when all filled, live is ok on testnet
    p.write_text(p.read_text().replace(": null", ": 5"))
    cfg2 = load_config(str(p))
    cfg2.assert_live_ready("rwa_positional")


def test_prod_requires_flag(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        """
env: prod
mode: live
rwa_positional:
  recommended_margin: 1000
  min_turnover_usd: 1
  leverage: 4
  margin_mode: cross
"""
    )
    cfg = load_config(str(p))
    with pytest.raises(ValueError):
        cfg.assert_live_ready("rwa_positional")
    cfg.flags["i_understand_this_is_live"] = True
    cfg.assert_live_ready("rwa_positional")
