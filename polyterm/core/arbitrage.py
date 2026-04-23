"""
Cross-Market Arbitrage Scanner

Tipos de arbitraje que realmente funcionan en Polymarket:
1. NegRisk (multi-outcome): sum(YES_i) < 1.0  ← principal oportunidad
2. Correlated markets: dos mercados sobre el mismo evento con precios distintos
3. Intra-market: YES_ask + NO_ask < 1.0  ← muy raro, requiere CLOB directo

NOTA SOBRE INTRA-MARKET ARB:
En Polymarket los tokens YES y NO de un mercado binario son complementarios.
El campo outcomePrices siempre suma 1.0 exactamente (NO = 1 - YES).
Para intra-market arb real hay que consultar el CLOB de ambos tokens
por separado. La oportunidad aparece cuando market makers de YES y NO
divergen, lo cual es raro en mercados líquidos pero ocurre en mercados
pequeños y en momentos de alta volatilidad.
"""

import asyncio
import json
import time
from typing import Dict, List, Optional, Any, Tuple, TYPE_CHECKING
from datetime import datetime, timedelta
from dataclasses import dataclass
from collections import defaultdict

from ..db.database import Database
from ..db.models import ArbitrageOpportunity
from ..api.gamma import GammaClient
from ..api.clob import CLOBClient

if TYPE_CHECKING:
    from .orderbook import OrderBookAnalyzer


@dataclass
class ArbitrageResult:
    """Resultado de una oportunidad de arbitraje."""
    type: str  # 'intra_market', 'correlated', 'cross_platform'
    market1_id: str
    market2_id: str
    market1_title: str
    market2_title: str
    market1_yes_price: float
    market1_no_price: float
    market2_yes_price: float = 0.0
    market2_no_price: float = 0.0
    spread: float = 0.0
    expected_profit_pct: float = 0.0
    expected_profit_usd: float = 0.0
    fees: float = 0.0
    net_profit: float = 0.0
    timestamp: datetime = None
    confidence: str = 'medium'

    # ── Mejoras: Sizing & Slippage ───────────────────────────────────────
    kelly_fraction: float = 0.0
    kelly_size_usd: float = 0.0
    slippage_pct: float = 0.0
    net_edge_after_slippage: float = 0.0

    # ── Tokens (necesarios para ejecución REAL) ──────────────────────────
    yes_token_id: str = ""
    no_token_id: str = ""

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class ArbitrageScanner:
    """
    Escanea oportunidades de arbitraje en Polymarket.

    La estrategia principal es NegRisk (usa NegRiskAnalyzer directamente).
    El scan intra-market busca divergencias reales consultando CLOB cuando
    hay OrderBookAnalyzer disponible, o usando bestAsk/outcomePrices como
    aproximación para mercados de baja liquidez.
    """

    def __init__(
        self,
        database: Database,
        gamma_client: GammaClient,
        clob_client: CLOBClient,
        min_spread: float = 0.025,
        polymarket_fee: float = 0.02,
        orderbook_analyzer: Optional["OrderBookAnalyzer"] = None,
        bankroll: float = 1000.0,
        max_kelly_fraction: float = 0.15,
    ):
        self.db = database
        self.gamma = gamma_client
        self.clob = clob_client
        self.min_spread = min_spread
        self.polymarket_fee = polymarket_fee
        self.ob_analyzer = orderbook_analyzer
        self.bankroll = bankroll
        self.max_kelly_fraction = max_kelly_fraction
        self.market_cache: Dict[str, Dict[str, Any]] = {}
        self.cache_ttl = 30

    # ── Kelly Criterion ───────────────────────────────────────────────────

    def _calculate_kelly(self, net_profit_fraction: float) -> tuple:
        """Half-Kelly para arbitraje."""
        if net_profit_fraction <= 0:
            return 0.0, 0.0
        half_kelly = net_profit_fraction * 0.5
        fraction   = min(half_kelly, self.max_kelly_fraction)
        return round(fraction, 4), round(fraction * self.bankroll, 2)

    # ── Slippage estimate ─────────────────────────────────────────────────

    def _estimate_slippage_from_ob(self, market: Dict[str, Any],
                                    size_usd: float = 100.0) -> float:
        """Estima slippage por liquidez disponible."""
        if not self.ob_analyzer:
            liquidity = float(market.get('liquidity', 0) or 0)
            if liquidity <= 0:       return 0.05
            elif liquidity < 1_000:  return 0.03
            elif liquidity < 10_000: return 0.015
            elif liquidity < 100_000:return 0.005
            else:                    return 0.002

        token_ids = self._extract_token_ids(market)
        if not token_ids:
            return 0.02

        half_size = size_usd / 2.0
        total_slip, sides = 0.0, 0

        for token_id in token_ids[:2]:
            ob = None
            try:
                ob = self.ob_analyzer.get_order_book_snapshot(token_id)
            except Exception:
                pass
            if not ob:
                total_slip += 0.01; sides += 1; continue

            asks = ob.get('asks', [])
            if not asks:
                total_slip += 0.01; sides += 1; continue

            best_price = float(asks[0].get('price', 0.5))
            if best_price <= 0:
                total_slip += 0.01; sides += 1; continue

            remaining, weighted, filled = half_size, 0.0, 0.0
            for level in asks:
                p = float(level.get('price', 0))
                s = float(level.get('size', 0))
                f = min(p * s, remaining)
                weighted += f * p; filled += f; remaining -= f
                if remaining <= 0: break

            slip = abs(weighted / filled - best_price) / best_price if filled > 0 else 0.02
            total_slip += slip; sides += 1

        return total_slip / sides if sides > 0 else 0.02

    # ── Token extraction ──────────────────────────────────────────────────

    def _extract_token_ids(self, market: Dict[str, Any]) -> List[str]:
        raw = market.get('clobTokenIds', [])
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = []
        return [str(t) for t in raw] if raw else []

    def _get_live_prices_for_market(self, market: Dict[str, Any]) -> Optional[Dict[str, float]]:
        if not self.ob_analyzer:
            return None
        token_ids = self._extract_token_ids(market)
        if not token_ids:
            return None
        live_data = self.ob_analyzer.get_live_prices(token_ids)
        if not live_data:
            return None
        yes_tid = token_ids[0] if len(token_ids) > 0 else None
        no_tid  = token_ids[1] if len(token_ids) > 1 else None
        yes_info = live_data.get(yes_tid) if yes_tid else None
        no_info  = live_data.get(no_tid)  if no_tid  else None
        yes_price = no_price = None
        if yes_info:
            yes_price = yes_info.get('mid_price') or yes_info.get('best_ask')
        if no_info:
            no_price = no_info.get('mid_price') or no_info.get('best_ask')
        if yes_price is not None and no_price is None:
            no_price = 1.0 - yes_price
        elif no_price is not None and yes_price is None:
            yes_price = 1.0 - no_price
        if yes_price is not None and no_price is not None:
            return {'yes': yes_price, 'no': no_price}
        return None

    def _get_clob_prices(self, market: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        """
        Obtiene precios ASK independientes para YES y NO desde el CLOB.

        Esto es lo que realmente necesitamos para detectar intra-market arb:
        precios de los dos tokens consultados por separado.
        Solo funciona cuando hay CLOBClient disponible.
        """
        token_ids = self._extract_token_ids(market)
        if len(token_ids) < 2:
            return None
        try:
            ob_yes = self.clob.get_order_book(token_ids[0])
            ob_no  = self.clob.get_order_book(token_ids[1])
            if not ob_yes or not ob_no:
                return None

            def best_ask(ob):
                asks = ob.get('asks', [])
                if not asks:
                    return None
                a = asks[0]
                return float(a['price'] if isinstance(a, dict) else a[0])

            yes_ask = best_ask(ob_yes)
            no_ask  = best_ask(ob_no)
            if yes_ask and no_ask:
                return yes_ask, no_ask
        except Exception:
            pass
        return None

    # ── Scan intra-market ─────────────────────────────────────────────────

    def scan_intra_market_arbitrage(
        self,
        markets: List[Dict[str, Any]],
    ) -> List[ArbitrageResult]:
        """
        Busca arb intra-market: YES_ask + NO_ask < 1.0

        Estrategia de precios (por orden de precisión):
          1. WebSocket live prices (más preciso)
          2. CLOB REST: fetch independiente de ambos tokens  ← el único correcto
          3. outcomePrices de Gamma: SIEMPRE suma 1.0, solo útil con spread negativo

        IMPORTANTE: outcomePrices[0] + outcomePrices[1] siempre = 1.0 en Polymarket
        porque NO = 1 - YES por construcción. Para ver arb real necesitas (2) o (1).

        El scan funciona mejor en mercados con:
          - Baja liquidez (< $5k)
          - Alta volatilidad reciente (oneDayPriceChange grande)
          - Spread anómalo negativo
        """
        opportunities = []

        for market in markets:
            # Soporta tanto mercados planos como anidados (event['markets'])
            items = market.get('markets', [market]) if isinstance(market, dict) else [market]

            for item in items:
                market_id = item.get('id', item.get('conditionId', ''))
                if not market_id:
                    continue

                yes_price = no_price = None

                # 1. WS live (más preciso)
                live = self._get_live_prices_for_market(item)
                if live:
                    yes_price = live['yes']
                    no_price  = live['no']

                # 2. CLOB REST directo (único modo que detecta arb binario real)
                if yes_price is None and self.clob:
                    clob_prices = self._get_clob_prices(item)
                    if clob_prices:
                        yes_price, no_price = clob_prices

                # 3. Gamma outcomePrices — solo si tiene spread negativo
                # (oneDayPriceChange muy negativo puede indicar desequilibrio)
                if yes_price is None:
                    op = item.get('outcomePrices', [])
                    if isinstance(op, str):
                        try: op = json.loads(op)
                        except Exception: continue
                    if len(op) < 2:
                        continue
                    try:
                        mid_yes = float(op[0])
                        mid_no  = float(op[1])
                    except (ValueError, TypeError):
                        continue

                    # outcomePrices suma 1.0 exactamente → ajustar por spread del CLOB
                    # bestAsk es ligeramente mayor que mid, lo usamos como YES ask
                    best_ask = item.get('bestAsk')
                    if best_ask is not None:
                        try:
                            yes_price = float(best_ask)
                            # NO ask: tomamos el precio real del NO token desde outcomePrices[1]
                            # ajustado por el spread (bid-ask del token YES aplicado simétricamente)
                            bid_ask_spread = float(item.get('spread', 0) or 0)
                            no_price = mid_no + (bid_ask_spread / 2.0)
                        except (ValueError, TypeError):
                            yes_price = mid_yes
                            no_price  = mid_no
                    else:
                        yes_price = mid_yes
                        no_price  = mid_no

                if yes_price is None or no_price is None:
                    continue

                total = yes_price + no_price
                if total >= (1.0 - self.min_spread):
                    continue

                spread           = 1.0 - total
                gross_profit_pct = (spread / total) * 100
                fee_on_winning   = self.polymarket_fee * (1.0 - min(yes_price, no_price))
                net_profit       = spread - fee_on_winning

                if net_profit <= 0:
                    continue

                title = item.get('question', item.get('title',
                        market.get('title', '')))
                result = ArbitrageResult(
                    type='intra_market',
                    market1_id=market_id,
                    market2_id=market_id,
                    market1_title=title,
                    market2_title=title,
                    market1_yes_price=yes_price,
                    market1_no_price=no_price,
                    spread=spread,
                    expected_profit_pct=gross_profit_pct,
                    expected_profit_usd=net_profit * 100,
                    fees=self.polymarket_fee * 100,
                    net_profit=net_profit * 100,
                    confidence='high' if spread > 0.05 else 'medium',
                )

                # Token IDs: críticos para ejecución REAL
                token_ids = self._extract_token_ids(item)
                if len(token_ids) >= 2:
                    result.yes_token_id = token_ids[0]
                    result.no_token_id = token_ids[1]
                    # Back-compat: algunas partes del sistema leen estos atributos privados
                    setattr(result, "_yes_token", token_ids[0])
                    setattr(result, "_no_token", token_ids[1])

                slippage       = self._estimate_slippage_from_ob(item, size_usd=100.0)
                net_after_slip = (net_profit / 100.0) - slippage
                kf, ku         = self._calculate_kelly(net_after_slip)
                result.slippage_pct            = round(slippage, 4)
                result.net_edge_after_slippage = round(net_after_slip * 100, 2)
                result.kelly_fraction          = kf
                result.kelly_size_usd          = ku

                if result.net_edge_after_slippage <= 0:
                    continue

                opportunities.append(result)
                self._store_opportunity(result)

        return sorted(opportunities, key=lambda x: x.net_profit, reverse=True)

    # ── Scan correlated markets ────────────────────────────────────────────

    def scan_correlated_markets(
        self,
        markets: List[Dict[str, Any]],
        similarity_threshold: float = 0.8,
    ) -> List[ArbitrageResult]:
        """
        Busca mercados correlacionados con precios divergentes.
        Agrupa por event slug/id y compara precios entre mercados del mismo evento.
        """
        from difflib import SequenceMatcher

        opportunities = []
        flat = []
        for m in markets:
            items = m.get('markets', [m])
            for item in items:
                if item.get('id') or item.get('conditionId'):
                    flat.append(item)

        # Agrupar por event
        by_event: Dict[str, List] = defaultdict(list)
        for item in flat:
            events = item.get('events', [])
            eid = None
            if isinstance(events, list) and events:
                eid = events[0].get('id') if isinstance(events[0], dict) else None
            if not eid:
                eid = item.get('groupItemTitle', item.get('slug', ''))
            if eid:
                by_event[str(eid)].append(item)

        for eid, group in by_event.items():
            if len(group) < 2:
                continue
            # Comparar pares dentro del mismo evento
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    m1, m2 = group[i], group[j]
                    op1 = m1.get('outcomePrices', [])
                    op2 = m2.get('outcomePrices', [])
                    if isinstance(op1, str):
                        try: op1 = json.loads(op1)
                        except Exception: continue
                    if isinstance(op2, str):
                        try: op2 = json.loads(op2)
                        except Exception: continue
                    if not op1 or not op2:
                        continue
                    try:
                        p1 = float(op1[0])
                        p2 = float(op2[0])
                    except (ValueError, IndexError):
                        continue

                    price_diff = abs(p1 - p2)
                    if price_diff < self.min_spread:
                        continue

                    # Títulos similares pero precios diferentes → posible arb
                    t1 = m1.get('question', m1.get('title', ''))
                    t2 = m2.get('question', m2.get('title', ''))
                    similarity = SequenceMatcher(None, t1.lower(), t2.lower()).ratio()

                    if similarity < similarity_threshold:
                        continue

                    spread     = price_diff
                    net_profit = spread - self.polymarket_fee
                    if net_profit <= 0:
                        continue

                    result = ArbitrageResult(
                        type='correlated',
                        market1_id=m1.get('id', ''),
                        market2_id=m2.get('id', ''),
                        market1_title=t1[:60],
                        market2_title=t2[:60],
                        market1_yes_price=p1,
                        market1_no_price=1-p1,
                        market2_yes_price=p2,
                        market2_no_price=1-p2,
                        spread=spread,
                        expected_profit_usd=net_profit * 100,
                        fees=self.polymarket_fee * 100,
                        net_profit=net_profit * 100,
                        confidence='medium',
                    )
                    kf, ku = self._calculate_kelly(net_profit)
                    result.kelly_fraction = kf
                    result.kelly_size_usd = ku
                    opportunities.append(result)

        return sorted(opportunities, key=lambda x: x.net_profit, reverse=True)

    # ── Store opportunity ─────────────────────────────────────────────────

    def _store_opportunity(self, result: ArbitrageResult) -> None:
        try:
            opp = ArbitrageOpportunity(
                market1_id=result.market1_id,
                market2_id=result.market2_id,
                market1_title=result.market1_title,
                market2_title=result.market2_title,
                market1_price=result.market1_yes_price,
                market2_price=result.market2_yes_price,
                spread=result.spread,
                expected_profit=result.net_profit,
                timestamp=result.timestamp,
                status='open',
            )
            self.db.save_arbitrage_opportunity(opp)
        except Exception:
            pass


# ── Kalshi cross-platform scanner ─────────────────────────────────────────────

class KalshiArbitrageScanner:
    """Scanner de arbitraje cross-platform Polymarket vs Kalshi."""

    def __init__(self, gamma_client: GammaClient, kalshi_api_key: str = "",
                 min_spread: float = 0.02):
        self.gamma       = gamma_client
        self.kalshi_key  = kalshi_api_key
        self.min_spread  = min_spread

    def scan(self, polymarket_markets: List[Dict]) -> List[ArbitrageResult]:
        """Compara precios entre Polymarket y Kalshi."""
        if not self.kalshi_key:
            return []
        # Implementación futura cuando haya API key de Kalshi
        return []
