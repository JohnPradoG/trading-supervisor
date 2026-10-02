"""Datos sintéticos para la fase 9: operaciones cerradas con su Trading DNA (y, opcional, los
hechos del análisis) insertadas directamente, con una trampa plantada o solo ruido."""

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from supervisor.models import (
    Account,
    AnalysisFinding,
    Bot,
    BotVersion,
    Broker,
    Trade,
    TradeAnalysis,
    TradeDna,
)
from supervisor.models.enums import (
    AccountType,
    DataOrigin,
    Direction,
    FindingKind,
    MarginMode,
    Outcome,
    TradeSource,
    TradeStatus,
)

START = datetime(2026, 1, 5, tzinfo=UTC)
SESSIONS = ("ASIA", "LONDRES", "LONDRES_NY", "NUEVA_YORK", "FUERA_SESION")
TRENDS = ("A_FAVOR", "EN_CONTRA", "LATERAL")


@dataclass
class Scope:
    account_id: uuid.UUID
    bot_id: uuid.UUID
    version_id: uuid.UUID
    symbol: str


def make_scope(session: Session) -> Scope:
    broker = Broker(name=f"Patrones-{uuid.uuid4().hex[:8]}")
    account = Account(
        broker=broker,
        login=random.randint(10_000_000, 99_999_999),
        server="Demo",
        currency="USD",
        account_type=AccountType.DEMO,
        margin_mode=MarginMode.HEDGING,
    )
    bot = Bot(name=f"EA_Patrones_{uuid.uuid4().hex[:6]}", strategy="sintética")
    session.add_all([broker, account, bot])
    session.flush()
    version = BotVersion(
        bot_id=bot.bot_id, version="1.0.0", main_timeframe="M15", params={}, released_at=START
    )
    session.add(version)
    session.flush()
    return Scope(account.id, bot.bot_id, version.id, f"SYN{uuid.uuid4().hex[:5].upper()}")


def trade_r(rng: random.Random, trap: bool, flipped: bool = False) -> float:
    """Trampa: gana 1 R el 10 % de las veces (-0.8 R de media). Resto: +1.5 R el 50 %
    (+0.25 R de media). `flipped`: la "trampa" pasa a ganar 1.5 R el 85 % (forward caducado)."""
    if trap and flipped:
        return 1.5 if rng.random() < 0.85 else -1.0
    if trap:
        return 1.0 if rng.random() < 0.1 else -1.0
    return 1.5 if rng.random() < 0.5 else -1.0


def insert_trades(
    session: Session,
    scope: Scope,
    n: int,
    *,
    seed: int,
    planted: bool,
    start_index: int = 0,
    facts: bool = False,
    flipped: bool = False,
) -> list[uuid.UUID]:
    """n operaciones (una por hora desde START + start_index horas). Con `facts` cada una
    lleva un análisis con PRECIO_VS_EMA200_H1 coherente con su tendencia H1."""
    rng = random.Random(seed)
    ids = []
    for i in range(start_index, start_index + n):
        trend = rng.choice(TRENDS)
        is_trap = planted and trend == "EN_CONTRA"
        r = trade_r(rng, is_trap, flipped)
        direction = rng.choice((Direction.BUY, Direction.SELL))
        entry = START + timedelta(hours=i)
        sl = Decimal("19900") if direction == Direction.BUY else Decimal("20100")
        # MAE en puntos (point 0.01, SL a 10000 puntos): las perdedoras llegan a 1 R.
        mae = 10_000 if r < 0 else rng.randint(500, 6000)
        trade = Trade(
            account_id=scope.account_id,
            bot_version_id=scope.version_id,
            position_id=random.randint(1, 2**62),
            magic_number=909,
            symbol=scope.symbol,
            direction=direction,
            order_type="MARKET",
            source=TradeSource.DEMO,
            status=TradeStatus.CLOSED,
            symbol_point=Decimal("0.01"),
            entry_time=entry,
            entry_price=Decimal("20000"),
            initial_sl=sl,
            initial_volume=Decimal("0.10"),
            max_volume=Decimal("0.10"),
            risk_amount=Decimal("100"),
            close_time=entry + timedelta(minutes=30),
            net_profit=Decimal(str(round(r * 100, 2))),
            mae_points=Decimal(mae),
            data_quality={},
        )
        session.add(trade)
        session.flush()
        features = {
            "tendencia_h1_rel": trend,
            "sesion": rng.choice(SESSIONS),
            "rsi14": round(rng.uniform(20, 80), 2),
            "bos_reciente": rng.random() < 0.5,
            "adx14": round(rng.uniform(10, 50), 2),
            "fvg_cercano": rng.random() < 0.4,
        }
        session.add(
            TradeDna(
                trade_id=trade.trade_id,
                dna_version=1,
                feature_set_version=1,
                input_hash=uuid.uuid4().hex * 2,
                source=DataOrigin.SERVER,
                data_cutoff=entry,
                features=features,
                null_reasons={},
                data_quality={},
                inputs={},
            )
        )
        if facts:
            analysis = TradeAnalysis(
                trade_id=trade.trade_id,
                analysis_version=1,
                outcome=Outcome.WIN if r > 0 else Outcome.LOSS,
                analyzer_version="test",
                ruleset_version=1,
                input_hash=uuid.uuid4().hex * 2,
                inputs={},
            )
            session.add(analysis)
            session.flush()
            session.add(
                AnalysisFinding(
                    analysis_id=analysis.id,
                    kind=FindingKind.FACT,
                    code="PRECIO_VS_EMA200_H1",
                    text="precio frente a la EMA200 H1",
                    evidence={"a_favor": trend != "EN_CONTRA"},
                )
            )
        ids.append(trade.trade_id)
    session.flush()
    return ids
