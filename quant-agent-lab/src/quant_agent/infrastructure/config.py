from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_agent.domain.models import to_dict

yaml: Any
try:
    import yaml as _yaml
except ImportError:  # pragma: no cover - the environment used for the MVP includes PyYAML.
    yaml = None
else:
    yaml = _yaml


@dataclass(frozen=True)
class StrategyConfig:
    strategy_id: str = "moving-average-demo"
    version: str = "1.0.0"
    fast_window: int = 3
    slow_window: int = 5
    minimum_strength: Decimal = Decimal("0.001")


@dataclass(frozen=True)
class PortfolioConfig:
    target_notional_per_signal: Decimal = Decimal("1000")
    lot_size: Decimal = Decimal("1")
    fee_bps: Decimal = Decimal("5")
    slippage_bps: Decimal = Decimal("10")
    currency: str = "USD"
    approval_ttl_seconds: int = 3600


@dataclass(frozen=True)
class RiskConfig:
    version: str = "risk-demo-v1"
    max_order_notional: Decimal = Decimal("2500")
    max_symbol_notional: Decimal = Decimal("5000")
    max_total_exposure: Decimal = Decimal("10000")
    min_cash_buffer: Decimal = Decimal("500")
    max_daily_turnover: Decimal = Decimal("5000")
    max_price_deviation_bps: Decimal = Decimal("500")
    max_data_age_seconds: int = 86400
    max_orders: int = 10
    kill_switch_default: bool = False


@dataclass(frozen=True)
class PaperBrokerConfig:
    default_fill_policy: str = "full"


@dataclass(frozen=True)
class LlmConfig:
    """LLM 决策层配置（key 走环境变量，绝不进配置文件）。"""
    api_url: str = ""
    model: str = ""
    api_key_env: str = "QUANT_AGENT_LLM_API_KEY"
    timeout: int = 60
    rag_url: str = "http://127.0.0.1:8010"
    rag_collection: str = "financial-reports"


@dataclass(frozen=True)
class MarketDataConfig:
    """行情数据源：file（本地 JSON/CSV，默认）/ live（自动拉取）。"""
    source: str = "file"          # file | live
    market: str = "us"            # us | cn | both
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class DemoConfig:
    version: str = "demo-v1"
    local_timezone: str = "Asia/Shanghai"
    strategy: StrategyConfig = StrategyConfig()
    portfolio: PortfolioConfig = PortfolioConfig()
    risk: RiskConfig = RiskConfig()
    paper_broker: PaperBrokerConfig = PaperBrokerConfig()
    llm: LlmConfig = LlmConfig()
    market_data: MarketDataConfig = MarketDataConfig()


def _decimal(data: dict[str, Any], key: str, default: Decimal) -> Decimal:
    return Decimal(str(data.get(key, default)))


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required to load project configuration")
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def load_demo_config(config_dir: Path | None = None) -> DemoConfig:
    directory = config_dir or (Path(__file__).resolve().parents[3] / "config")
    demo = _load_yaml(directory / "demo.yaml")
    risk = _load_yaml(directory / "risk.demo.yaml")
    strategy_data = demo.get("strategy", {})
    portfolio_data = demo.get("portfolio", {})
    broker_data = demo.get("paper_broker", {})
    return DemoConfig(
        version=str(demo.get("version", "demo-v1")),
        local_timezone=str(demo.get("local_timezone", "Asia/Shanghai")),
        strategy=StrategyConfig(
            strategy_id=str(strategy_data.get("id", "moving-average-demo")),
            version=str(strategy_data.get("version", "1.0.0")),
            fast_window=int(strategy_data.get("fast_window", 3)),
            slow_window=int(strategy_data.get("slow_window", 5)),
            minimum_strength=_decimal(strategy_data, "minimum_strength", Decimal("0.001")),
        ),
        portfolio=PortfolioConfig(
            target_notional_per_signal=_decimal(
                portfolio_data, "target_notional_per_signal", Decimal("1000")
            ),
            lot_size=_decimal(portfolio_data, "lot_size", Decimal("1")),
            fee_bps=_decimal(portfolio_data, "fee_bps", Decimal("5")),
            slippage_bps=_decimal(portfolio_data, "slippage_bps", Decimal("10")),
            currency=str(portfolio_data.get("currency", "USD")),
            approval_ttl_seconds=int(portfolio_data.get("approval_ttl_seconds", 3600)),
        ),
        risk=RiskConfig(
            version=str(risk.get("version", "risk-demo-v1")),
            max_order_notional=_decimal(risk, "max_order_notional", Decimal("2500")),
            max_symbol_notional=_decimal(risk, "max_symbol_notional", Decimal("5000")),
            max_total_exposure=_decimal(risk, "max_total_exposure", Decimal("10000")),
            min_cash_buffer=_decimal(risk, "min_cash_buffer", Decimal("500")),
            max_daily_turnover=_decimal(risk, "max_daily_turnover", Decimal("5000")),
            max_price_deviation_bps=_decimal(risk, "max_price_deviation_bps", Decimal("500")),
            max_data_age_seconds=int(risk.get("max_data_age_seconds", 86400)),
            max_orders=int(risk.get("max_orders", 10)),
            kill_switch_default=bool(risk.get("kill_switch_default", False)),
        ),
        paper_broker=PaperBrokerConfig(
            default_fill_policy=str(broker_data.get("default_fill_policy", "full"))
        ),
        llm=LlmConfig(
            api_url=str(demo.get("llm", {}).get("api_url", "")),
            model=str(demo.get("llm", {}).get("model", "")),
            api_key_env=str(demo.get("llm", {}).get(
                "api_key_env", "QUANT_AGENT_LLM_API_KEY")),
            timeout=int(demo.get("llm", {}).get("timeout", 60)),
            rag_url=str(demo.get("llm", {}).get(
                "rag_url", "http://127.0.0.1:8010")),
            rag_collection=str(demo.get("llm", {}).get(
                "rag_collection", "financial-reports")),
        ),
        market_data=MarketDataConfig(
            source=str(demo.get("market_data", {}).get("source", "file")),
            market=str(demo.get("market_data", {}).get("market", "us")),
            symbols=tuple(demo.get("market_data", {}).get("symbols", []) or []),
        ),
    )


def config_dict(config: DemoConfig) -> dict[str, Any]:
    return to_dict(config)
