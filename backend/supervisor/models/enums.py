"""Valores cerrados del dominio. Se guardan como texto con CHECK en la base de datos."""

from enum import StrEnum


class AccountType(StrEnum):
    DEMO = "DEMO"
    REAL = "REAL"
    CONTEST = "CONTEST"


class MarginMode(StrEnum):
    HEDGING = "HEDGING"
    NETTING = "NETTING"
    EXCHANGE = "EXCHANGE"


class BotStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    RETIRED = "RETIRED"


class RawEventStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    IGNORED = "IGNORED"


class TradeSource(StrEnum):
    LIVE = "LIVE"
    DEMO = "DEMO"
    BACKTEST = "BACKTEST"
    FORWARD = "FORWARD"
    IMPORTED = "IMPORTED"


class TradeStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class TradeEventType(StrEnum):
    TRADE_OPENED = "TRADE_OPENED"
    SL_MODIFIED = "SL_MODIFIED"
    TP_MODIFIED = "TP_MODIFIED"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    REENTRY = "REENTRY"
    POSITION_INCREASED = "POSITION_INCREASED"
    POSITION_DECREASED = "POSITION_DECREASED"
    TRADE_CLOSED = "TRADE_CLOSED"


class ExitReason(StrEnum):
    SL = "SL"
    TP = "TP"
    MANUAL = "MANUAL"
    EXPERT = "EXPERT"
    STOP_OUT = "STOP_OUT"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class ExcursionSource(StrEnum):
    EA_TICKS = "EA_TICKS"
    M1_BARS = "M1_BARS"


class DataOrigin(StrEnum):
    EA = "EA"
    SERVER = "SERVER"


class Outcome(StrEnum):
    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"


class FindingKind(StrEnum):
    FACT = "FACT"
    HYPOTHESIS = "HYPOTHESIS"


class HypothesisStatus(StrEnum):
    PROPOSED = "PROPOSED"
    TESTING = "TESTING"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    DECAYED = "DECAYED"


class SplitSegment(StrEnum):
    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    OOS = "OOS"
    FORWARD = "FORWARD"


class ExperimentStatus(StrEnum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
