"""NegRisk Multi-Outcome Arbitrage Detection

Detects arbitrage in multi-outcome (NegRisk) markets where the sum of all
outcome YES prices doesn't equal $1.00.

In NegRisk markets, multiple outcomes are mutually exclusive (e.g., "Who wins
the election?" with 5+ candidates). The sum of all YES prices should be $1.00.
"""

import json
import logging

logger = logging.getLogger(__name__)
from typing import Dict, List, Optional, Any
from datetime import datetime

from ..api.gamma import GammaClient
from ..api.clob import CLOBClient
from ..utils.json_output import safe_float


class NegRiskAnalyzer:
    """Analyze multi-outcome NegRisk markets for arbitrage"""

    def __init__(self, gamma_client, clob_client=None, polymarket_fee=0.02,
                 bankroll=1000.0, max_kelly_fraction=0.10,
                 min_outcome_liquidity: float = 50.0):
        self.gamma = gamma_client
        self.clob = clob_client
        self.polymarket_fee = polymarket_fee
        self.bankroll = bankroll
        self.max_kelly_fraction = max_kelly_fraction
        self.min_outcome_liquidity = min_outcome_liquidity  # $ mínimos en ask por outcome
        self._liquidity_cache: dict = {}   # {token_id: (ts, liquidity_usd)}
        self._cache_ttl = 60               # segundos

    def _extract_event_reference(self, market):
        """Extract event key and metadata from a flat market row."""
        event_data = {}
        events = market.get("events")
        if isinstance(events, list) and events:
            first_event = events[0]
            if isinstance(first_event, dict):
                event_data = first_event
        elif isinstance(market.get("event"), dict):
            event_data = market.get("event", {})

        event_key = (
            event_data.get("id")
            or market.get("eventId")
            or market.get("event_id")
            or event_data.get("slug")
            or market.get("eventSlug")
            or market.get("event_slug")
        )

        if event_key is None:
            return None, {}

        return str(event_key), event_data

    def _extract_token_id(self, market):
        """Extract first CLOB token id safely from list or JSON string."""
        token_ids = market.get("clobTokenIds", [])
        if isinstance(token_ids, list):
            return str(token_ids[0]) if token_ids else ""
        if isinstance(token_ids, str):
            try:
                parsed = json.loads(token_ids)
            except Exception:
                return ""
            if isinstance(parsed, list) and parsed:
                return str(parsed[0])
        return ""

    def find_multi_outcome_events(self, limit=50):
        """Find events with 3+ outcome markets (NegRisk candidates)

        Returns list of events that have 3+ markets (multi-outcome)
        """
        rows = self.gamma.get_markets(limit=limit * 3, active=True, closed=False)
        multi = []

        # Backward-compatible path for callers/tests that already pass nested
        # event payloads with `event["markets"]`.
        for event in rows:
            markets = event.get('markets', [])
            if isinstance(markets, list) and len(markets) >= 3:
                multi.append(event)
            if len(multi) >= limit:
                return multi

        # Production Gamma `/markets` payload is flat: group markets by event.
        grouped_events = {}
        for market in rows:
            if not isinstance(market, dict):
                continue
            if isinstance(market.get("markets"), list):
                continue

            event_key, event_data = self._extract_event_reference(market)
            if event_key is None:
                continue

            if event_key not in grouped_events:
                grouped_events[event_key] = {
                    "id": event_data.get("id")
                    or market.get("eventId")
                    or market.get("event_id")
                    or event_key,
                    "title": event_data.get("title")
                    or market.get("eventTitle")
                    or market.get("event_title")
                    or market.get("title")
                    or market.get("question", ""),
                    "markets": [],
                }

            grouped_events[event_key]["markets"].append(market)
            if not grouped_events[event_key]["title"]:
                grouped_events[event_key]["title"] = (
                    event_data.get("title")
                    or market.get("title")
                    or market.get("question", "")
                )

        for event in grouped_events.values():
            if len(event.get("markets", [])) >= 3:
                multi.append(event)
            if len(multi) >= limit:
                break

        return multi[:limit]


    def _get_ask_liquidity(self, token_id: str, market: dict = None) -> float:
        """
        Devuelve la liquidez disponible para comprar este outcome.

        Estrategia en orden de fiabilidad:
          1. CLOB order book (más preciso — liquidez ask real)
          2. Campo 'liquidity' / 'liquidityNum' de Gamma (fallback fiable)
          3. Campo 'volume24hr' de Gamma como proxy mínimo
          4. 0.0 si no hay ningún dato

        Args:
            token_id: ID del token YES del outcome
            market:   Diccionario del mercado Gamma (para fallback)

        Returns:
            Liquidez estimada en USD.
        """
        import time as _time

        # ── Intento 1: CLOB order book ────────────────────────────────────
        if token_id and self.clob:
            cached = self._liquidity_cache.get(token_id)
            if cached and (_time.time() - cached[0]) < self._cache_ttl:
                clob_liq = cached[1]
            else:
                clob_liq = 0.0
                try:
                    ob = self.clob.get_order_book(token_id)
                    if ob:
                        asks = ob.get('asks', [])
                        for level in asks[:10]:
                            if isinstance(level, dict):
                                price = float(level.get('price', 0) or 0)
                                size  = float(level.get('size',  0) or 0)
                            else:
                                price = float(level[0]) if len(level) > 0 else 0
                                size  = float(level[1]) if len(level) > 1 else 0
                            clob_liq += price * size
                except Exception:
                    clob_liq = 0.0
                self._liquidity_cache[token_id] = (_time.time(), clob_liq)

            if clob_liq > 0:
                return clob_liq

        # ── Intento 2: campo liquidity de Gamma ───────────────────────────
        if market:
            liq = market.get('liquidityNum') or market.get('liquidity') or market.get('liquidityClob')
            if liq:
                try:
                    return float(liq)
                except (ValueError, TypeError):
                    pass

            # Intento 3: volume24hr como proxy (si hay volumen, hay liquidez)
            vol = market.get('volume24hr') or market.get('volume24hrClob')
            if vol:
                try:
                    return float(vol) * 0.1   # estimamos 10% del vol diario como liquidez
                except (ValueError, TypeError):
                    pass

        return 0.0

    def _kelly_negrisk(self, net_profit_fraction: float, num_outcomes: int) -> tuple:
        """Kelly Criterion para NegRisk multi-outcome.

        Args:
            net_profit_fraction: Ganancia neta / inversión
            num_outcomes: Número de outcomes a comprar

        Returns:
            (kelly_fraction, kelly_size_usd)
        """
        if net_profit_fraction <= 0 or num_outcomes < 2:
            return 0.0, 0.0
        implied_odds = 1.0 / max(num_outcomes - 1, 1)
        full_kelly   = net_profit_fraction / implied_odds if implied_odds > 0 else 0
        half_kelly   = full_kelly * 0.5
        fraction     = min(half_kelly, self.max_kelly_fraction)
        return round(fraction, 4), round(fraction * self.bankroll, 2)

    def analyze_event(self, event):
        """Analyze a multi-outcome event for NegRisk arbitrage

        NegRisk property: In a complete set of mutually exclusive outcomes,
        the sum of all YES prices should equal $1.00.
        If sum < $1.00: buy all outcomes (guaranteed profit on resolution)
        If sum > $1.00: overpriced (potential short opportunity)
        """
        markets = event.get('markets', [])
        if len(markets) < 2:
            return None

        outcomes = []
        total_yes = 0.0

        for market in markets:
            outcome_prices = market.get('outcomePrices', [])
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except Exception:
                    continue

            if not outcome_prices:
                continue

            yes_price = safe_float(outcome_prices[0])
            question = market.get('question', market.get('groupItemTitle', ''))
            token_id = self._extract_token_id(market)

            # Consultar liquidez: CLOB primero, Gamma como fallback
            ask_liquidity = self._get_ask_liquidity(token_id, market=market)

            outcomes.append({
                'question':     question[:60],
                'yes_price':    yes_price,
                'market_id':    market.get('id', market.get('conditionId', '')),
                'token_id':     token_id,
                'ask_liquidity': round(ask_liquidity, 2),
                'is_liquid':    ask_liquidity >= self.min_outcome_liquidity,
            })
            total_yes += yes_price

        if not outcomes:
            return None

        # ── Sanity check: suma debe estar cerca de 1.0 ───────────────────────
        # Si sum > 1.5 los outcomes NO son mutuamente excluyentes (falso NegRisk)
        # Si sum < 0.3 hay datos corruptos o mercados casi resueltos
        raw_sum = sum(o['yes_price'] for o in outcomes)
        if raw_sum > 1.5 or raw_sum < 0.3:
            logger.debug(
                "Descartando evento '%s': sum=%.4f fuera de rango [0.3, 1.5] "
                "(outcomes probablemente no mutuamente excluyentes)",
                event.get('title', '')[:45], raw_sum
            )
            return None

        # ── Verificación de liquidez ──────────────────────────────────────────
        liquid_outcomes   = [o for o in outcomes if o['is_liquid']]
        illiquid_outcomes = [o for o in outcomes if not o['is_liquid']]
        n_illiquid        = len(illiquid_outcomes)

        # Si hay outcomes ilíquidos: recalcular con el total real ejecutable.
        # Si todos son ilíquidos: marcar como no ejecutable pero seguir mostrando.
        if illiquid_outcomes:
            # Recalcular total_yes solo con outcomes líquidos para saber
            # si el arb sigue siendo válido sin los ilíquidos.
            liquid_total = sum(o['yes_price'] for o in liquid_outcomes) if liquid_outcomes else total_yes

        # Capacidad máxima ejecutable = mínimo de liquidez entre todos los outcomes.
        # No tiene sentido invertir más de lo que el menos líquido permite comprar.
        if outcomes:
            min_liquidity_usd = min(o['ask_liquidity'] for o in outcomes)
        else:
            min_liquidity_usd = 0.0

        spread = abs(1.0 - total_yes)

        # Calculate fee-adjusted profit for underpriced case
        # Buy all YES outcomes for $total_yes, get $1.00 back guaranteed
        # Fee: 2% on winnings of the ONE outcome that resolves YES
        # Winning = 1.0 - cheapest_outcome_price (worst case for fees)
        cheapest = min(o['yes_price'] for o in outcomes) if outcomes else 0
        fee_on_winning = self.polymarket_fee * (1.0 - cheapest) if cheapest < 1.0 else 0

        if total_yes < 1.0:
            net_profit = (1.0 - total_yes) - fee_on_winning
        else:
            net_profit = -(total_yes - 1.0)  # Loss if overpriced

        # Log de diagnóstico de liquidez
        event_title = event.get('title', '')[:45]
        if illiquid_outcomes:
            logger.warning(
                "NegRisk '%s': %d/%d outcomes ilíquidos (min=$%.0f, umbral=$%.0f) → %s",
                event_title, n_illiquid, len(outcomes),
                min_liquidity_usd, self.min_outcome_liquidity,
                "BLOQUEADO" if not (n_illiquid == 0) else "OK"
            )
            for o in illiquid_outcomes[:3]:  # log primeros 3 ilíquidos
                logger.warning("  Ilíquido: '%s' liq=$%.2f token=%s",
                               o['question'][:40], o['ask_liquidity'], o['token_id'][:12])
        else:
            logger.info("NegRisk '%s': todos %d outcomes líquidos (min=$%.0f) ✅",
                        event_title, len(outcomes), min_liquidity_usd)

        # Kelly capeado también por la liquidez mínima del mercado
        kelly_frac, kelly_usd = self._kelly_negrisk(
            net_profit_fraction=net_profit,
            num_outcomes=len(outcomes),
        )
        # No invertir más de lo que permiten los outcomes menos líquidos
        kelly_usd = min(kelly_usd, min_liquidity_usd) if min_liquidity_usd > 0 else kelly_usd

        # Ejecutable solo si:
        #   a) TODOS los outcomes tienen liquidez suficiente
        #   b) El kelly_usd resultante es > $1 (hay capital mínimo ejecutable)
        is_executable = (n_illiquid == 0) and (min_liquidity_usd >= self.min_outcome_liquidity)

        return {
            'event_title':        event.get('title', ''),
            'event_id':           event.get('id', ''),
            'num_outcomes':       len(outcomes),
            'num_liquid':         len(liquid_outcomes),
            'num_illiquid':       n_illiquid,
            'min_liquidity_usd':  round(min_liquidity_usd, 2),
            'is_executable':      is_executable,
            'total_yes_price':    round(total_yes, 4),
            'spread':             round(spread, 4),
            'type':               'underpriced' if total_yes < 1.0 else 'overpriced',
            'fee_adjusted_profit': round(net_profit, 4),
            'profit_per_100':     round(net_profit * 100, 2),
            'kelly_fraction':     kelly_frac,
            'kelly_size_usd':     kelly_usd,
            'outcomes':           outcomes,
            'timestamp':          datetime.now().isoformat(),
        }

    def scan_all(self, min_spread=0.02, only_executable=True):
        """Scan all NegRisk events for arbitrage opportunities.

        Args:
            min_spread:       Spread mínimo para incluir (default 2%)
            only_executable:  Si True (default), solo devuelve oportunidades
                              donde TODOS los outcomes tienen liquidez suficiente.
                              Si False, devuelve todas (incluyendo ilíquidas).

        Returns:
            List of arbitrage opportunities sorted by profit potential
        """
        events = self.find_multi_outcome_events(limit=50)
        opportunities = []

        for event in events:
            result = self.analyze_event(event)
            if not result:
                continue
            if result['spread'] < min_spread:
                continue
            # Solo ejecutar oportunidades underpriced (sum < 1.0)
            # Las overpriced (sum > 1.0) requieren shorting, no soportado aún
            if only_executable and result['type'] != 'underpriced':
                continue
            if only_executable and not result['is_executable']:
                continue
            opportunities.append(result)

        return sorted(opportunities, key=lambda x: x['fee_adjusted_profit'], reverse=True)
