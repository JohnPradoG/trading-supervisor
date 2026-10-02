"""Motor de análisis (fase 6): resultado de cada operación, hechos medidos (FACT), hipótesis
no validadas (HYPOTHESIS) y estadísticas.

Módulos puros (sin base de datos, probados con valores calculados a mano):
- outcome: GANADORA / PERDEDORA / BREAKEVEN con tolerancia documentada.
- sessions: sesión, día y hora de mercado, siempre desde UTC.
- indicators: EMA, ATR y percentiles.
- statistics: métricas de una serie de operaciones cerradas.
- rules: reglas declarativas y versionadas que generan hipótesis a partir de hechos.

Con base de datos:
- market: velas M1 agregadas en SQL a M15/H1 (solo velas cerradas antes de la entrada).
- facts: hechos de una operación a partir de sus datos, eventos y velas.
- analyzer: crea versiones nuevas de trade_analyses cuando cambian las entradas.
"""
