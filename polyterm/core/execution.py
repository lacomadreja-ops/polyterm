"""
polyterm/core/execution.py
==========================
Motor de ejecución de órdenes para arbitraje — paper y real.

Respeta la arquitectura de PolyTerm:
  - Se integra con ArbitrageResult (polyterm.core.arbitrage)
  - Usa Database (polyterm.db.database) para el journal
  - Logging estándar con getLogger(__name__)
  - Documentación Google-style

Dependencia opcional para modo real:
    pip install py-clob-client
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from .arbitrage import ArbitrageResult
    from ..db.database import Database

logger = logging.getLogger(__name__)

# ─── Constantes ──────────────────────────────────────────────────────────────

TAKER_FEE_RATE = 0.002   # 0.2% por orden (market order)
MAKER_FEE_RATE = 0.000   # 0% para limit orders
DEFAULT_CHAIN_ID = 137    # Polygon


# ─── Enums y dataclasses ─────────────────────────────────────────────────────

class ExecutionMode(Enum):
    """Modo de ejecución del bot."""
    PAPER = "paper"   # Simula sin capital real
    REAL  = "real"    # Ejecuta órdenes reales


@dataclass
class TradeExecution:
    """Resultado de ejecutar un lado del arb (YES o NO)."""
    mode: str
    market_id: str
    token_id: str
    outcome: str          # "YES" | "NO"
    side: str             # "BUY"
    size_usd: float
    price: float
    fill_price: float
    fees_usd: float
    success: bool
    order_id: Optional[str] = None
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    @property
    def pnl_estimate(self) -> float:
        """PnL estimado en USD (positivo = ganancia esperada)."""
        if not self.success:
            return 0.0
        return self.size_usd * 0.01  # placeholder hasta resolución


@dataclass
class ArbExecution:
    """Par de ejecuciones (YES + NO) para un arb completo."""
    opportunity_id: str
    market_id: str
    market_title: str
    yes_trade: TradeExecution
    no_trade: TradeExecution
    spread: float
    net_edge: float
    kelly_fraction: float
    total_invested: float
    expected_net_profit: float
    timestamp: float = field(default_factory=time.time)

    @property
    def both_success(self) -> bool:
        return self.yes_trade.success and self.no_trade.success

    @property
    def any_failure(self) -> bool:
        return not self.yes_trade.success or not self.no_trade.success


# ─── Ejecutor principal ───────────────────────────────────────────────────────

class ArbExecutor:
    """
    Ejecuta pares de órdenes para capturar spreads de arbitraje.

    En modo PAPER simula con slippage realista y registra en la Database
    de PolyTerm. En modo REAL usa py-clob-client para colocar órdenes
    limit en el CLOB de Polymarket.

    Usage::

        executor = ArbExecutor(
            database=db,
            mode=ExecutionMode.PAPER,
            bankroll=1000.0,
            max_size_usd=50.0,
        )
        result = await executor.execute(arb_result)
        if result.both_success:
            print(f"Arb ejecutado: ${result.total_invested:.2f}")

    Args:
        database: Instancia de Database (polyterm.db.database)
        mode: PAPER o REAL
        bankroll: Capital total disponible en USD
        max_size_usd: Tamaño máximo por arb en USD
        slippage_tolerance: Máxima desviación de precio aceptada (0.01 = 1%)
    """

    def __init__(
        self,
        database: "Database",
        mode: ExecutionMode = ExecutionMode.PAPER,
        bankroll: float = 1000.0,
        max_size_usd: float = 50.0,
        slippage_tolerance: float = 0.01,
        config=None,
    ):
        self.db = database
        self.mode = mode
        self.bankroll = bankroll
        self.max_size_usd = max_size_usd
        self.slippage_tolerance = slippage_tolerance
        self._clob_client = None
        self._config = config  # Config de PolyTerm (carga .env automáticamente)

        if mode == ExecutionMode.REAL:
            self._init_clob_client()

    # ── Setup ────────────────────────────────────────────────────────────────

    def _init_clob_client(self):
        """Inicializa el cliente CLOB real (py-clob-client).

        Lee las credenciales en este orden de prioridad:
          1. Config de PolyTerm (que ya ha cargado el .env)
          2. Variables de entorno del sistema como fallback
        """
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            # Leer desde Config si está disponible (ya tiene el .env cargado)
            if self._config is not None:
                creds_dict   = self._config.trading_credentials()
                private_key  = creds_dict["private_key"]
                api_key      = creds_dict["api_key"]
                api_secret   = creds_dict["api_secret"]
                api_pass     = creds_dict["api_passphrase"]
                chain_id     = creds_dict["chain_id"]
            else:
                # Fallback: variables de entorno directas
                private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
                api_key     = os.getenv("POLYMARKET_API_KEY", "")
                api_secret  = os.getenv("POLYMARKET_API_SECRET", "")
                api_pass    = os.getenv("POLYMARKET_API_PASSPHRASE", "")
                chain_id    = int(os.getenv("POLYMARKET_CHAIN_ID", str(DEFAULT_CHAIN_ID)))

            if not private_key:
                raise ValueError(
                    "POLYMARKET_PRIVATE_KEY no configurada. "
                    "Añádela al fichero .env en la raíz del proyecto."
                )

            creds = None
            if api_key:
                creds = ApiCreds(
                    api_key        = api_key,
                    api_secret     = api_secret,
                    api_passphrase = api_pass,
                )

            self._clob_client = ClobClient(
                host     = "https://clob.polymarket.com",
                key      = private_key,
                chain_id = chain_id,
                creds    = creds,
            )
            logger.info("Cliente CLOB real inicializado correctamente")

        except ImportError:
            raise ImportError(
                "py-clob-client no instalado. "
                "Ejecuta: pip install py-clob-client"
            )
        except Exception as e:
            raise RuntimeError(f"Error inicializando cliente CLOB real: {e}")

    # ── Ejecución pública ────────────────────────────────────────────────────

    async def execute(self, opp: "ArbitrageResult") -> ArbExecution:
        """
        Ejecuta un par YES + NO para capturar el spread de arbitraje.

        Determina el tamaño usando Kelly (capped a max_size_usd),
        ejecuta ambos lados en paralelo y registra en la Database.

        Args:
            opp: ArbitrageResult con kelly_size_usd y slippage_pct calculados

        Returns:
            ArbExecution con los resultados de ambos lados
        """
        # Tamaño: Kelly size pero nunca más del máximo configurado
        kelly_usd = getattr(opp, 'kelly_size_usd', self.max_size_usd)
        size = min(kelly_usd, self.max_size_usd)

        # Extraer token IDs del mercado
        yes_token, no_token = self._get_tokens(opp)

        if self.mode == ExecutionMode.PAPER:
            execution = await self._paper_execute(opp, size, yes_token, no_token)
        else:
            execution = await self._real_execute(opp, size, yes_token, no_token)

        # Persistir en la Database de PolyTerm
        self._persist_execution(execution)

        return execution

    # ── Ejecución paper ──────────────────────────────────────────────────────

    async def _paper_execute(
        self,
        opp: "ArbitrageResult",
        size: float,
        yes_token: str,
        no_token: str,
    ) -> ArbExecution:
        """Simula ejecución con slippage realista."""
        slip = getattr(opp, 'slippage_pct', 0.005) / 2  # por lado

        yes_fill = opp.market1_yes_price * (1 + slip)
        no_fill  = opp.market1_no_price  * (1 + slip)

        yes_trade = TradeExecution(
            mode       = "paper",
            market_id  = opp.market1_id,
            token_id   = yes_token,
            outcome    = "YES",
            side       = "BUY",
            size_usd   = size / 2,
            price      = opp.market1_yes_price,
            fill_price = yes_fill,
            fees_usd   = (size / 2) * TAKER_FEE_RATE,
            success    = True,
            order_id   = f"PAPER-YES-{int(time.time())}",
        )
        no_trade = TradeExecution(
            mode       = "paper",
            market_id  = opp.market1_id,
            token_id   = no_token,
            outcome    = "NO",
            side       = "BUY",
            size_usd   = size / 2,
            price      = opp.market1_no_price,
            fill_price = no_fill,
            fees_usd   = (size / 2) * TAKER_FEE_RATE,
            success    = True,
            order_id   = f"PAPER-NO-{int(time.time())}",
        )

        net_edge = getattr(opp, 'net_edge_after_slippage', opp.net_profit)
        kelly_frac = getattr(opp, 'kelly_fraction', 0.0)

        logger.info(
            "[PAPER] Arb: %s | Edge=%.3f%% | Size=$%.2f | Kelly=%.1f%%",
            opp.market1_title[:45],
            net_edge,
            size,
            kelly_frac * 100,
        )

        return ArbExecution(
            opportunity_id  = f"{opp.market1_id}:{opp.timestamp.isoformat()}",
            market_id       = opp.market1_id,
            market_title    = opp.market1_title,
            yes_trade       = yes_trade,
            no_trade        = no_trade,
            spread          = opp.spread,
            net_edge        = net_edge,
            kelly_fraction  = kelly_frac,
            total_invested  = size,
            expected_net_profit = net_edge / 100 * size,
        )

    # ── Ejecución real ───────────────────────────────────────────────────────

    async def _real_execute(
        self,
        opp: "ArbitrageResult",
        size: float,
        yes_token: str,
        no_token: str,
    ) -> ArbExecution:
        """Coloca órdenes reales en paralelo via py-clob-client."""
        if not self._clob_client:
            raise RuntimeError("Cliente CLOB no inicializado")

        # Ejecutar ambos lados en paralelo para minimizar slippage temporal
        yes_task = asyncio.create_task(
            self._place_order(yes_token, "BUY", size / 2, opp.market1_yes_price, opp, "YES")
        )
        no_task = asyncio.create_task(
            self._place_order(no_token, "BUY", size / 2, opp.market1_no_price, opp, "NO")
        )

        yes_result, no_result = await asyncio.gather(
            yes_task, no_task, return_exceptions=True
        )

        # Manejar errores parciales
        if isinstance(yes_result, Exception):
            logger.error("Orden YES fallida: %s", yes_result)
            yes_result = TradeExecution(
                mode="real", market_id=opp.market1_id, token_id=yes_token,
                outcome="YES", side="BUY", size_usd=size/2,
                price=opp.market1_yes_price, fill_price=0.0, fees_usd=0.0,
                success=False, error=str(yes_result),
            )

        if isinstance(no_result, Exception):
            logger.error("Orden NO fallida: %s", no_result)
            no_result = TradeExecution(
                mode="real", market_id=opp.market1_id, token_id=no_token,
                outcome="NO", side="BUY", size_usd=size/2,
                price=opp.market1_no_price, fill_price=0.0, fees_usd=0.0,
                success=False, error=str(no_result),
            )

        net_edge   = getattr(opp, 'net_edge_after_slippage', opp.net_profit)
        kelly_frac = getattr(opp, 'kelly_fraction', 0.0)

        execution = ArbExecution(
            opportunity_id  = f"{opp.market1_id}:{opp.timestamp.isoformat()}",
            market_id       = opp.market1_id,
            market_title    = opp.market1_title,
            yes_trade       = yes_result,
            no_trade        = no_result,
            spread          = opp.spread,
            net_edge        = net_edge,
            kelly_fraction  = kelly_frac,
            total_invested  = size if yes_result.success and no_result.success else 0.0,
            expected_net_profit = net_edge / 100 * size,
        )

        if execution.both_success:
            logger.info(
                "[REAL] ✅ Arb ejecutado: %s | Edge=%.3f%% | $%.2f",
                opp.market1_title[:45], net_edge, size,
            )
        else:
            logger.warning(
                "[REAL] ⚠️  Arb parcial: %s | YES=%s NO=%s",
                opp.market1_title[:45],
                "OK" if yes_result.success else "FAIL",
                "OK" if no_result.success else "FAIL",
            )

        return execution

    async def _place_order(
        self,
        token_id: str,
        side: str,
        size_usd: float,
        ref_price: float,
        opp: "ArbitrageResult",
        outcome: str,
    ) -> TradeExecution:
        """Coloca una orden limit via py-clob-client."""
        try:
            from py_clob_client.clob_types import OrderArgs

            order_args = OrderArgs(
                token_id = token_id,
                price    = round(ref_price, 4),
                size     = round(size_usd / ref_price, 2),  # USD → shares
                side     = side,
            )

            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._clob_client.create_and_post_order(order_args),
            )

            order_id = resp.get("orderID", resp.get("id", "unknown"))

            return TradeExecution(
                mode       = "real",
                market_id  = opp.market1_id,
                token_id   = token_id,
                outcome    = outcome,
                side       = side,
                size_usd   = size_usd,
                price      = ref_price,
                fill_price = ref_price,  # actualizar si disponible en resp
                fees_usd   = size_usd * TAKER_FEE_RATE,
                success    = True,
                order_id   = order_id,
            )

        except Exception as e:
            raise RuntimeError(f"Error colocando orden {outcome} {token_id[:8]}: {e}")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _get_tokens(self, opp: "ArbitrageResult") -> tuple:
        """Extrae token IDs YES y NO del mercado."""
        yes_token = (
            getattr(opp, "yes_token_id", "")
            or getattr(opp, "_yes_token", "")
        )
        no_token = (
            getattr(opp, "no_token_id", "")
            or getattr(opp, "_no_token", "")
        )

        # Fallback legacy (solo tolerable en PAPER)
        if not yes_token:
            yes_token = getattr(opp, "market1_id", "") + ":YES"
        if not no_token:
            no_token = getattr(opp, "market1_id", "") + ":NO"

        # En REAL, no aceptamos placeholders.
        if self.mode == ExecutionMode.REAL:
            if ":" in yes_token or ":" in no_token:
                raise RuntimeError(
                    "Token IDs inválidos para modo REAL. "
                    "Asegura que ArbitrageResult tenga yes_token_id/no_token_id reales (clobTokenIds)."
                )
        return yes_token, no_token

    def _persist_execution(self, execution: ArbExecution):
        """Guarda la ejecución en la tabla arb_executions de PolyTerm."""
        try:
            self.db.save_arb_execution(execution)
        except AttributeError:
            # Si la DB no tiene el método aún (antes del migration)
            logger.debug("Database.save_arb_execution no disponible aún. Ver PATCH_3.")
        except Exception as e:
            logger.warning("Error persistiendo ejecución: %s", e)

    # ── Stats ────────────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """Resumen de performance desde la database."""
        try:
            return self.db.get_execution_summary()
        except AttributeError:
            return {"error": "Ejecuta PATCH_3 para habilitar el journal"}
