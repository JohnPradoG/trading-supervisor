"""Importa todos los modelos para que Base.metadata contenga el esquema completo."""

from supervisor.models.analysis import (
    AnalysisFinding,
    FeatureDefinition,
    NewsEvent,
    TradeAnalysis,
    TradeDna,
)
from supervisor.models.base import Base
from supervisor.models.bots import Bot, BotVersion, Deployment
from supervisor.models.identity import Account, ApiClient, Broker, Terminal, User
from supervisor.models.research import (
    Alert,
    BacktestRun,
    DataSplit,
    Experiment,
    ExperimentResult,
    Hypothesis,
    PatternRun,
    PatternTest,
)
from supervisor.models.trading import (
    AccountSnapshot,
    Heartbeat,
    PriceBar,
    RawEvent,
    Trade,
    TradeEvent,
)

__all__ = [
    "Base",
    "Broker",
    "Account",
    "Terminal",
    "ApiClient",
    "User",
    "Bot",
    "BotVersion",
    "Deployment",
    "RawEvent",
    "Trade",
    "TradeEvent",
    "AccountSnapshot",
    "Heartbeat",
    "PriceBar",
    "FeatureDefinition",
    "TradeDna",
    "TradeAnalysis",
    "AnalysisFinding",
    "NewsEvent",
    "Hypothesis",
    "DataSplit",
    "PatternRun",
    "PatternTest",
    "Experiment",
    "BacktestRun",
    "ExperimentResult",
    "Alert",
]
