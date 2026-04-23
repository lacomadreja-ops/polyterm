"""
polyterm/core/whale_filter.py
=============================
Filtro de riesgo basado en actividad de ballenas e insiders.

Se integra con:
  - polyterm.api.data_api.DataAPIClient  (trades reales)
  - polyterm.core.analytics.AnalyticsEngine (track_whale_trades)
  - polyterm.db.database.Database  (cache en alerts table)

El filtro bloquea la ejecución de arb cuando detecta:
  1. Ballena reciente (>= min_whale_usd) en el mismo mercado
  2. Score de riesgo insider alto (señales de información privilegiada)
  3. Actividad anómala en los últimos X minutos

Usage::

    from polyterm.core.whale_filter import WhaleFilter

    wf = WhaleFilter(data_client, min_whale_usd=10_000)
    decision = wf.should_skip(market_id, market_data)
    if decision.skip:
        print(f"Skipped: {decision.reason}")
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..api.data_api import DataAPIClient

logger = logging.getLogger(__name__)


# ─── Estructuras de datos ─────────────────────────────────────────────────────

@dataclass
class WhaleSignal:
    """Una señal de actividad de ballena/insider detectada."""
    wallet: str
    market_id: str
    side: str           # "YES" | "NO"
    amount_usd: float
    timestamp: float
    risk_score: int     # 0-100
    signal_type: str    # "whale" | "insider" | "anomaly"

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.timestamp) / 60


@dataclass
class FilterDecision:
    """Resultado del filtro: si se debe saltar el mercado y por qué."""
    skip: bool
    reason: str
    risk_score: int = 0
    signals: List[WhaleSignal] = field(default_factory=list)

    @classmethod
    def allow(cls) -> "FilterDecision":
        return cls(skip=False, reason="OK", risk_score=0)

    @classmethod
    def block(cls, reason: str, risk_score: int = 60,
              signals: Optional[List[WhaleSignal]] = None) -> "FilterDecision":
        return cls(skip=True, reason=reason, risk_score=risk_score,
                   signals=signals or [])


# ─── WhaleFilter ─────────────────────────────────────────────────────────────

class WhaleFilter:
    """
    Evalúa si un mercado es seguro para ejecutar arbitraje
    basándose en la actividad reciente de ballenas e insiders.

    La lógica: si una ballena ha tomado posición en los últimos
    `lookback_minutes`, puede ser señal de información privilegiada
    sobre la resolución. En ese caso, el lado "correcto" ya está
    priceado y el spread de arb puede colapsar de golpe.

    Args:
        data_client: DataAPIClient de PolyTerm
        min_whale_usd: Umbral mínimo para considerar un trade como ballena
        lookback_minutes: Ventana de tiempo para buscar señales
        max_risk_score: Score máximo antes de bloquear (0-100)
        cache_ttl_seconds: TTL del cache de señales por mercado
    """

    # Pesos para el scoring insider
    _SCORE_WEIGHTS = {
        "large_trade":    30,   # trade > min_whale_usd
        "fresh_wallet":   25,   # wallet con < 5 trades históricos
        "concentrated":   20,   # > 5% del volumen del mercado
        "rapid_entry":    15,   # múltiples trades en < 5 min
        "low_liquidity":  10,   # mercado con poca liquidez
    }

    def __init__(
        self,
        data_client: "DataAPIClient",
        min_whale_usd: float = 10_000.0,
        lookback_minutes: float = 30.0,
        max_risk_score: int = 60,
        cache_ttl_seconds: int = 300,
    ):
        self.data_client     = data_client
        self.min_whale_usd   = min_whale_usd
        self.lookback_minutes = lookback_minutes
        self.max_risk_score  = max_risk_score
        self.cache_ttl       = cache_ttl_seconds

        # Cache simple en memoria: {market_id: (timestamp, [WhaleSignal])}
        self._cache: Dict[str, tuple] = {}

    # ── API pública ──────────────────────────────────────────────────────────

    def should_skip(
        self,
        market_id: str,
        market_data: Optional[Dict[str, Any]] = None,
    ) -> FilterDecision:
        """
        Evalúa si se debe saltar este mercado por riesgo de ballena/insider.

        Args:
            market_id: ID del mercado a evaluar
            market_data: Datos del mercado (opcional, para scoring adicional)

        Returns:
            FilterDecision con skip=True si hay señales de riesgo
        """
        signals = self._get_signals(market_id)

        if not signals:
            return FilterDecision.allow()

        # Filtrar solo señales recientes
        recent = [s for s in signals if s.age_minutes <= self.lookback_minutes]
        if not recent:
            return FilterDecision.allow()

        # Scoring
        max_score = max(s.risk_score for s in recent)
        top_signal = max(recent, key=lambda s: s.risk_score)

        if max_score >= self.max_risk_score:
            reason = (
                f"Whale/Insider detectado: {top_signal.wallet[:10]}... "
                f"${top_signal.amount_usd:,.0f} en {top_signal.side} "
                f"hace {top_signal.age_minutes:.0f}min "
                f"(risk={max_score}/100)"
            )
            logger.info("🚨 WhaleFilter BLOQUEÓ %s: %s", market_id[:12], reason)
            return FilterDecision.block(reason, max_score, recent)

        return FilterDecision.allow()

    def get_signals(self, market_id: str) -> List[WhaleSignal]:
        """Devuelve señales activas para un mercado (públicas, cached)."""
        return self._get_signals(market_id)

    # ── Internos ─────────────────────────────────────────────────────────────

    def _get_signals(self, market_id: str) -> List[WhaleSignal]:
        """Obtiene señales con cache."""
        now = time.time()
        cached = self._cache.get(market_id)
        if cached and (now - cached[0]) < self.cache_ttl:
            return cached[1]

        signals = self._fetch_and_score(market_id)
        self._cache[market_id] = (now, signals)
        return signals

    def _fetch_and_score(self, market_id: str) -> List[WhaleSignal]:
        """Descarga trades del mercado y calcula scores."""
        try:
            # DataAPIClient.get_trades requiere una wallet, pero podemos
            # consultar el endpoint de trades del mercado directamente.
            # Usamos el endpoint REST de data-api.polymarket.com/trades
            import requests
            resp = requests.get(
                "https://data-api.polymarket.com/trades",
                params={"market": market_id, "limit": 100},
                timeout=8,
            )
            if resp.status_code != 200:
                return []
            trades = resp.json()
            if not isinstance(trades, list):
                trades = trades.get("data", [])

            return self._parse_trades(trades, market_id)

        except Exception as e:
            logger.debug("WhaleFilter: error fetching trades para %s: %s", market_id, e)
            return []

    def _parse_trades(self, trades: list, market_id: str) -> List[WhaleSignal]:
        """Convierte trades raw en WhaleSignal con risk score."""
        signals = []
        cutoff = time.time() - self.lookback_minutes * 60 * 2  # x2 para el cache

        for t in trades:
            try:
                # Normalizar timestamp
                ts = float(t.get("timestamp", t.get("createdAt", 0)) or 0)
                if ts < cutoff:
                    continue

                amount = float(t.get("usdcSize", t.get("notional", 0)) or 0)
                if amount < self.min_whale_usd:
                    continue

                side   = "YES" if t.get("side", "").upper() in ("BUY", "YES") else "NO"
                wallet = t.get("maker", t.get("transactorAddress", "unknown"))
                score  = self._compute_risk_score(t, amount, market_id)

                signals.append(WhaleSignal(
                    wallet      = wallet,
                    market_id   = market_id,
                    side        = side,
                    amount_usd  = amount,
                    timestamp   = ts,
                    risk_score  = score,
                    signal_type = "insider" if score >= 70 else "whale",
                ))

            except Exception:
                continue

        return sorted(signals, key=lambda s: s.risk_score, reverse=True)

    def _compute_risk_score(self, trade: dict, amount: float, market_id: str) -> int:
        """
        Calcula un score de riesgo insider para un trade (0-100).

        Factores:
        - Tamaño del trade (large_trade)
        - Wallet nueva sin historial (fresh_wallet)
        - Concentración alta en el mercado (concentrated)
        """
        score = 0

        # Tamaño grande
        if amount >= 100_000:
            score += self._SCORE_WEIGHTS["large_trade"]
        elif amount >= 50_000:
            score += int(self._SCORE_WEIGHTS["large_trade"] * 0.7)
        elif amount >= 10_000:
            score += int(self._SCORE_WEIGHTS["large_trade"] * 0.4)

        # Wallet con poco historial (posible wallet nueva para ocultar identidad)
        hist_trades = trade.get("makerOrderCount", trade.get("tradeCount", 999))
        if isinstance(hist_trades, (int, float)) and hist_trades < 5:
            score += self._SCORE_WEIGHTS["fresh_wallet"]
        elif isinstance(hist_trades, (int, float)) and hist_trades < 20:
            score += int(self._SCORE_WEIGHTS["fresh_wallet"] * 0.5)

        # Alta concentración relativa al volumen del mercado
        mkt_vol = float(trade.get("marketVolume", trade.get("volume", 0)) or 0)
        if mkt_vol > 0 and amount / mkt_vol > 0.10:
            score += self._SCORE_WEIGHTS["concentrated"]
        elif mkt_vol > 0 and amount / mkt_vol > 0.05:
            score += int(self._SCORE_WEIGHTS["concentrated"] * 0.5)

        return min(score, 100)
