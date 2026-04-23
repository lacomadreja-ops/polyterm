"""
polyterm/cli/commands/trade.py
==============================
Nuevo comando CLI: ``polyterm trade``

Orquesta todo el pipeline de arbitraje:
  ArbitrageScanner → WhaleFilter → ArbExecutor → Database journal

Sigue exactamente los patrones de PolyTerm:
  - Click commands con @click.command()
  - Rich Console para output
  - Config via ctx.obj["config"]
  - GammaClient + CLOBClient inicializados igual que en otros comandos
  - handle_api_error() para errores
  - Soporte --format json

Registro en polyterm/cli/main.py:
    from .commands.trade import trade
    cli.add_command(trade)
"""

import asyncio
import sys
import time
from datetime import datetime
import logging

import click
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...api.gamma import GammaClient
from ...api.clob import CLOBClient
from ...api.data_api import DataAPIClient
from ...core.arbitrage import ArbitrageScanner
from ...core.negrisk import NegRiskAnalyzer
from ...core.execution import ArbExecutor, ExecutionMode
from ...core.whale_filter import WhaleFilter
from ...core.orderbook import OrderBookAnalyzer
from ...db.database import Database
from ...utils.json_output import print_json
from ...utils.errors import handle_api_error, show_error
from ...utils.config import Config

logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--mode",
    type=click.Choice(["paper", "real"]),
    default="paper",
    show_default=True,
    help="paper = simula sin riesgo  |  real = órdenes reales (¡cuidado!)",
)
@click.option("--bankroll", default=1000.0, show_default=True,
              help="Capital total disponible en USD")
@click.option("--min-edge", "min_edge", default=0.005, show_default=True,
              help="Edge neto mínimo para ejecutar (ej: 0.005 = 0.5%)")
@click.option("--max-size", "max_size", default=50.0, show_default=True,
              help="Tamaño máximo por arb en USD")
@click.option("--min-liquidity", "min_liquidity", default=1000.0, show_default=True,
              help="Liquidez mínima del mercado en USD para considerar")
@click.option("--min-spread", "min_spread", default=0.01, show_default=True,
              help="Spread bruto mínimo para escanear (ej: 0.01 = 1%)")
@click.option("--scan-interval", "scan_interval", default=30, show_default=True,
              help="Segundos entre scans REST")
@click.option("--no-whale-filter", "no_whale_filter", is_flag=True,
              help="Desactivar filtro de ballenas (no recomendado)")
@click.option("--whale-min-usd", "whale_min_usd", default=10_000.0, show_default=True,
              help="Umbral mínimo USD para considerar una ballena")
@click.option(
    "--negrisk/--no-negrisk",
    "include_negrisk",
    default=True,
    show_default=True,
    help="Activar escaneo NegRisk (recomendado como estrategia primaria)",
)
@click.option("--once", is_flag=True,
              help="Ejecutar un solo scan y salir (sin loop)")
@click.option("--limit", default=300, show_default=True,
              help="Número de mercados a escanear")
@click.option("--debug", is_flag=True,
              help="Mostrar todos los spreads aunque no sean ejecutables")
@click.option("--journal", "show_journal", is_flag=True,
              help="Ver historial de ejecuciones y salir")
@click.option("--format", "output_format",
              type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def trade(
    ctx,
    mode, bankroll, min_edge, max_size, min_liquidity, min_spread,
    scan_interval, no_whale_filter, whale_min_usd, include_negrisk,
    once, limit, debug, show_journal, output_format,
):
    """Arbitrage trading bot — detecta y ejecuta spreads YES+NO.

    Combina todas las mejoras en un pipeline completo:

    \b
    1. Scan REST de mercados Polymarket
    2. Filtro de liquidez y slippage estimado
    3. Sizing Kelly (half-Kelly conservador)
    4. Filtro de ballenas/insiders
    5. Ejecución paper o real
    6. Journal en la DB de PolyTerm (ver POLYTERM_DIR; por defecto /logs)

    \b
    Ejemplos:
        polyterm trade                             # Paper, $1000 bankroll
        polyterm trade --mode real --bankroll 500  # Real (¡pide confirmación!)
        polyterm trade --once --format json        # Un scan, JSON output
        polyterm trade --include-negrisk           # Incluye NegRisk
    """
    console = Console()
    config  = ctx.obj["config"]
    db      = Database()
    logger.info(
        "trade_start mode=%s bankroll=%.2f min_edge=%.4f max_size=%.2f min_spread=%.4f limit=%d negrisk=%s whale_filter=%s",
        mode, bankroll, min_edge, max_size, min_spread, limit, include_negrisk, (not no_whale_filter),
    )

    # ── Journal mode ──────────────────────────────────────────────────────
    if show_journal:
        _display_journal(console, db, output_format)
        return

    # ── Advertencia modo real ─────────────────────────────────────────────
    if mode == "real":
        console.print()
        console.print(Panel(
            "[bold red]⚠️  MODO REAL ACTIVADO[/bold red]\n\n"
            "Este bot ejecutará órdenes reales en Polymarket.\n"
            "Necesitas configurar las variables de entorno:\n\n"
            "  [cyan]POLYMARKET_PRIVATE_KEY[/cyan]\n"
            "  [cyan]POLYMARKET_API_KEY[/cyan]\n"
            "  [cyan]POLYMARKET_API_SECRET[/cyan]\n"
            "  [cyan]POLYMARKET_API_PASSPHRASE[/cyan]\n\n"
            "[yellow]Empieza con --max-size 10 para validar antes de escalar.[/yellow]",
            border_style="red",
        ))
        confirm = click.prompt(
            "Escribe 'SI' para confirmar",
            default="",
        )
        if confirm.strip().upper() != "SI":
            console.print("[yellow]Cancelado.[/yellow]")
            return
        console.print()

    # ── Inicializar clientes ──────────────────────────────────────────────
    gamma_client = GammaClient(
        base_url=config.gamma_base_url,
        api_key=config.gamma_api_key,
    )
    clob_client = CLOBClient(
        rest_endpoint=config.clob_rest_endpoint,
    )
    data_client = DataAPIClient()
    db          = Database()
    ob_analyzer = OrderBookAnalyzer(clob_client)

    # ── Inicializar módulos de mejora ─────────────────────────────────────
    scanner = ArbitrageScanner(
        database          = db,
        gamma_client      = gamma_client,
        clob_client       = clob_client,
        min_spread        = min_spread,
        orderbook_analyzer= ob_analyzer,
        bankroll          = bankroll,             # NUEVO: Kelly sizing
        max_kelly_fraction= 0.15,                 # NUEVO: cap 15%
    )

    executor = ArbExecutor(
        database     = db,
        mode         = ExecutionMode.REAL if mode == "real" else ExecutionMode.PAPER,
        bankroll     = bankroll,
        max_size_usd = max_size,
        config       = config,   # pasa Config para que lea el .env automáticamente
    )

    whale_filter = None if no_whale_filter else WhaleFilter(
        data_client      = data_client,
        min_whale_usd    = whale_min_usd,
        lookback_minutes = 30,
        max_risk_score   = 60,
    )

    negrisk = NegRiskAnalyzer(
        gamma_client      = gamma_client,
        clob_client       = clob_client,
        polymarket_fee    = 0.02,
    ) if include_negrisk else None

    # ── Cabecera ──────────────────────────────────────────────────────────
    if output_format != "json":
        mode_color = "red" if mode == "real" else "green"
        console.print()
        console.print(Panel(
            f"[bold]PolyTerm Arbitrage Trader[/bold]\n"
            f"Modo: [{mode_color}]{mode.upper()}[/{mode_color}]  "
            f"Bankroll: [cyan]${bankroll:,.0f}[/cyan]  "
            f"Min Edge: [yellow]{min_edge:.1%}[/yellow]  "
            f"Max Size: [cyan]${max_size:.0f}[/cyan]",
            border_style="cyan",
        ))
        console.print("[dim]Ctrl+C para detener[/dim]\n")

    # ── Loop principal ────────────────────────────────────────────────────
    stats = {"scans": 0, "opps": 0, "executed": 0, "skipped_whale": 0, "skipped_edge": 0}
    start = time.time()

    try:
        while True:
            stats["scans"] += 1

            # ── Fetch markets ──────────────────────────────────────────
            markets = gamma_client.get_markets(active=True, closed=False, limit=limit)
            if not markets:
                if output_format != "json":
                    show_error(console, "no_markets")
                break

            # ── Intra-market arb scan ──────────────────────────────────
            opps = scanner.scan_intra_market_arbitrage(markets)
            opps = [o for o in opps if o.net_profit >= min_edge * 100]

            # ── NegRisk scan ───────────────────────────────────────────
            negrisk_opps = []
            if negrisk:
                negrisk_opps = negrisk.scan_all(min_spread=min_spread)

            stats["opps"] += len(opps) + len(negrisk_opps)

            # ── Procesar oportunidades ─────────────────────────────────
            all_results = []
            for opp in opps:
                result = asyncio.run(_process_opportunity(
                    opp, executor, whale_filter, min_edge, stats,
                    dedupe_always=True,
                ))
                if result:
                    all_results.append(result)

            # ── Ejecutar NegRisk ───────────────────────────────────────
            nr_results = []
            for nr_opp in negrisk_opps:
                profit_ok  = nr_opp.get("fee_adjusted_profit", 0) >= min_edge
                liquid_ok  = nr_opp.get("is_executable", False)
                if profit_ok and liquid_ok and not debug:
                    nr_ex = _execute_negrisk_paper(
                        nr_opp, executor, max_size, stats,
                        dedupe_always=True,
                    )
                    if nr_ex:
                        nr_results.append(nr_ex)

            # ── Output ────────────────────────────────────────────────
            if output_format == "json":
                _output_json(stats, opps, negrisk_opps, all_results + nr_results)
            else:
                _display_table(console, opps, negrisk_opps, all_results + nr_results, stats, start, debug, min_edge)

            if once:
                break

            time.sleep(scan_interval)

    except KeyboardInterrupt:
        pass
    finally:
        # ── Reporte final ──────────────────────────────────────────────
        if output_format != "json":
            _display_final_report(console, executor, stats, start)

        gamma_client.close()
        clob_client.close()
        data_client.close()


# ─── Pipeline async de oportunidad ──────────────────────────────────────────

async def _process_opportunity(
    opp,
    executor,
    whale_filter,
    min_edge,
    stats,
    dedupe_always: bool = True,
):
    """Procesa una oportunidad: filtrar → ejecutar → retornar resultado."""
    db = getattr(executor, "db", None)
    opp_id = f"{getattr(opp, 'market1_id', '')}:{getattr(getattr(opp, 'timestamp', None), 'isoformat', lambda: '')()}"
    market_id = getattr(opp, "market1_id", "") or getattr(opp, "market_id", "")
    title = getattr(opp, "market1_title", "") or getattr(opp, "market_title", "")

    # Filtro de edge neto post-slippage
    net_edge = getattr(opp, 'net_edge_after_slippage', opp.net_profit)
    if net_edge < min_edge * 100:
        stats["skipped_edge"] += 1
        if db and hasattr(db, "save_arb_decision"):
            db.save_arb_decision(
                opportunity_id=opp_id,
                market_id=market_id,
                market_title=title,
                decision="SKIP",
                reason="edge_below_threshold",
                data={
                    "net_edge": net_edge,
                    "min_edge_pct": min_edge * 100,
                    "spread": getattr(opp, "spread", None),
                    "slippage_pct": getattr(opp, "slippage_pct", None),
                    "kelly_usd": getattr(opp, "kelly_size_usd", None),
                },
            )
        return None

    # Filtro de ballenas
    if whale_filter:
        decision = whale_filter.should_skip(opp.market1_id)
        if decision.skip:
            stats["skipped_whale"] += 1
            if db and hasattr(db, "save_arb_decision"):
                db.save_arb_decision(
                    opportunity_id=opp_id,
                    market_id=market_id,
                    market_title=title,
                    decision="SKIP",
                    reason="whale_filter",
                    data={
                        "net_edge": net_edge,
                        "reason": getattr(decision, "reason", ""),
                        "risk_score": getattr(decision, "risk_score", None),
                    },
                )
            return None

    # Deduplicación estricta: si ya hubo ejecución en este market_id, no re-ejecutar jamás.
    try:
        if dedupe_always and db and hasattr(db, "get_last_execution_for_market"):
            last = db.get_last_execution_for_market(market_id)
            if last:
                if db and hasattr(db, "save_arb_decision"):
                    db.save_arb_decision(
                        opportunity_id=opp_id,
                        market_id=market_id,
                        market_title=title,
                        decision="SKIP",
                        reason="already_executed_market",
                        data={
                            "last_execution": last,
                            "net_edge": net_edge,
                        },
                    )
                return None
    except Exception:
        # No bloquear trading por fallo de dedupe check
        pass

    # Ejecutar
    try:
        if db and hasattr(db, "save_arb_decision"):
            db.save_arb_decision(
                opportunity_id=opp_id,
                market_id=market_id,
                market_title=title,
                decision="EXECUTE",
                reason="pre_execute",
                data={
                    "net_edge": net_edge,
                    "spread": getattr(opp, "spread", None),
                    "slippage_pct": getattr(opp, "slippage_pct", None),
                    "kelly_usd": getattr(opp, "kelly_size_usd", None),
                    "kelly_fraction": getattr(opp, "kelly_fraction", None),
                },
            )
        execution = await executor.execute(opp)
        if execution.both_success:
            stats["executed"] += 1
        if db and hasattr(db, "save_arb_decision"):
            db.save_arb_decision(
                opportunity_id=getattr(execution, "opportunity_id", opp_id),
                market_id=getattr(execution, "market_id", market_id),
                market_title=getattr(execution, "market_title", title),
                decision="EXECUTE",
                reason="post_execute_ok" if execution.both_success else "post_execute_partial_or_fail",
                data={
                    "both_success": bool(execution.both_success),
                    "total_invested": getattr(execution, "total_invested", None),
                    "net_edge": getattr(execution, "net_edge", None),
                    "expected_profit": getattr(execution, "expected_net_profit", None),
                    "yes_order_id": getattr(getattr(execution, "yes_trade", None), "order_id", None),
                    "no_order_id": getattr(getattr(execution, "no_trade", None), "order_id", None),
                    "yes_error": getattr(getattr(execution, "yes_trade", None), "error", None),
                    "no_error": getattr(getattr(execution, "no_trade", None), "error", None),
                },
            )
        return execution
    except Exception as e:
        logger.error("Error ejecutando arb: %s", e)
        if db and hasattr(db, "save_arb_decision"):
            db.save_arb_decision(
                opportunity_id=opp_id,
                market_id=market_id,
                market_title=title,
                decision="ERROR",
                reason="exec_exception",
                data={"error": str(e)},
            )
        return None


# ─── Display helpers ─────────────────────────────────────────────────────────

def _display_table(console, opps, negrisk_opps, results, stats, start_time, debug=False, min_edge=0.005):
    """Muestra tabla de oportunidades con Rich."""
    console.rule(f"[dim]Scan #{stats['scans']} — {datetime.now().strftime('%H:%M:%S')}[/dim]")

    if not opps and not negrisk_opps:
        console.print("[dim]  Sin oportunidades este ciclo.[/dim]")
        return

    # Tabla intra-market
    if opps:
        table = Table(title=f"Arbitraje Intra-Market ({len(opps)} opps)", box=None)
        table.add_column("Mercado", style="green", max_width=42)
        table.add_column("Spread", justify="right", style="yellow")
        table.add_column("Slip", justify="right", style="dim")
        table.add_column("Net Edge", justify="right", style="bold")
        table.add_column("Kelly $", justify="right", style="magenta")
        table.add_column("Estado", justify="center")

        for opp in opps[:15]:
            slip  = getattr(opp, 'slippage_pct', 0.0)
            nedge = getattr(opp, 'net_edge_after_slippage', opp.net_profit)
            kelly = getattr(opp, 'kelly_size_usd', 0.0)

            edge_color = "green" if nedge > 0 else "red"
            conf_icon  = "🟢" if opp.confidence == "high" else "🟡" if opp.confidence == "medium" else "🔴"

            table.add_row(
                opp.market1_title[:42],
                f"{opp.spread:.2%}",
                f"{slip:.2%}",
                f"[{edge_color}]{nedge:+.2f}$[/{edge_color}]",
                f"${kelly:.0f}",
                f"{conf_icon} {opp.confidence}",
            )

        console.print(table)
        console.print()

    # Tabla NegRisk
    if negrisk_opps:
        nr_table = Table(title=f"NegRisk ({len(negrisk_opps)} opps)", box=None)
        nr_table.add_column("Evento",       style="cyan", max_width=35)
        nr_table.add_column("Out",          justify="right", width=4)
        nr_table.add_column("Líq.",         justify="right", width=5)
        nr_table.add_column("Min Liq $",    justify="right", width=9)
        nr_table.add_column("Sum",          justify="right", width=7)
        nr_table.add_column("P&L/$100",     justify="right", style="bold", width=9)
        nr_table.add_column("Kelly $",      justify="right", style="magenta", width=8)
        nr_table.add_column("Estado",       justify="center", width=11)

        for nr in negrisk_opps[:8]:
            pnl    = nr.get("fee_adjusted_profit", 0)
            is_exe = nr.get("is_executable", False)
            n_liq  = nr.get("num_liquid", nr.get("num_outcomes", 0))
            n_tot  = nr.get("num_outcomes", 0)
            minliq = nr.get("min_liquidity_usd", 0.0)
            executed = any(
                getattr(r, "market_id", "") == nr.get("event_id","") for r in results
            )

            pnl_color  = "green"  if pnl >= min_edge   else "yellow" if pnl > 0 else "red"
            liq_color  = "green"  if is_exe             else "red"
            liq_str    = f"[{liq_color}]{n_liq}/{n_tot}[/{liq_color}]"

            if executed:
                estado = "[green]EJECUTADO[/green]"
            elif is_exe:
                estado = "[green]✅ líquido[/green]"
            elif n_liq == 0:
                estado = "[red]❌ sin liq.[/red]"
            else:
                estado = f"[yellow]⚠ {n_tot-n_liq} ilíq.[/yellow]"

            nr_table.add_row(
                nr["event_title"][:35],
                str(n_tot),
                liq_str,
                f"${minliq:.0f}",
                f"{nr['total_yes_price']:.4f}",
                f"[{pnl_color}]${nr['profit_per_100']:.2f}[/{pnl_color}]",
                f"${nr.get('kelly_size_usd', 0):.0f}",
                estado,
            )

        console.print(nr_table)
        console.print()

    # Stats rápidas
    console.print(
        f"[dim]Ejecutados: {stats['executed']} │ "
        f"Skipped whale: {stats['skipped_whale']} │ "
        f"Skipped edge: {stats['skipped_edge']}[/dim]"
    )


def _output_json(stats, opps, negrisk_opps, results):
    """Output en formato JSON."""
    print_json({
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "stats": stats,
        "intramarket_opportunities": [
            {
                "market_id":    o.market1_id,
                "title":        o.market1_title,
                "spread":       o.spread,
                "slippage":     getattr(o, "slippage_pct", 0),
                "net_edge":     getattr(o, "net_edge_after_slippage", o.net_profit),
                "kelly_usd":    getattr(o, "kelly_size_usd", 0),
                "kelly_frac":   getattr(o, "kelly_fraction", 0),
                "confidence":   o.confidence,
            }
            for o in opps
        ],
        "negrisk_opportunities": negrisk_opps,
        "executions": [
            {
                "market_id":       r.market_id,
                "market_title":    r.market_title,
                "both_success":    r.both_success,
                "total_invested":  r.total_invested,
                "expected_profit": r.expected_net_profit,
                "spread":          r.spread,
                "net_edge":        r.net_edge,
                "kelly_fraction":  r.kelly_fraction,
                "yes_order":       r.yes_trade.order_id,
                "no_order":        r.no_trade.order_id,
            }
            for r in results if r
        ],
    })


def _display_final_report(console, executor, stats, start_time):
    """Muestra el reporte final al hacer Ctrl+C."""
    elapsed = (time.time() - start_time) / 60
    console.print()
    console.print(Panel(
        f"[bold]Reporte Final[/bold]\n"
        f"Tiempo activo:  [cyan]{elapsed:.1f} min[/cyan]\n"
        f"Scans:          [cyan]{stats['scans']}[/cyan]\n"
        f"Opps detectadas:[cyan]{stats['opps']}[/cyan]\n"
        f"Arbs ejecutados:[cyan]{stats['executed']}[/cyan]\n"
        f"Skip whale:     [yellow]{stats['skipped_whale']}[/yellow]\n"
        f"Skip edge:      [yellow]{stats['skipped_edge']}[/yellow]",
        border_style="green",
        title="PolyTerm Trade",
    ))

    # Resumen de DB
    try:
        summary = executor.get_summary()
        if summary.get("total_arbs", 0) > 0:
            console.print()
            console.print(
                f"[bold]P&L Estimado:[/bold] "
                f"${summary.get('expected_pnl', 0):.4f} | "
                f"Avg Edge: {summary.get('avg_edge', 0):.3f}% | "
                f"Capital desplegado: ${summary.get('total_invested', 0):.2f}"
            )
    except Exception:
        pass

    console.print()
    from ...utils.paths import get_polyterm_dir
    console.print(f"[dim]Datos guardados en {get_polyterm_dir()}/data.db[/dim]")
    console.print("[dim]Consulta el journal: polyterm trade --journal[/dim]")


# ─── NegRisk paper execution ─────────────────────────────────────────────────

def _execute_negrisk_paper(
    nr_opp: dict,
    executor,
    max_size: float,
    stats: dict,
    dedupe_always: bool = True,
):
    """
    Simula la ejecución de un arb NegRisk en papel.
    Compra todos los outcomes YES del evento.
    Registra en el journal como un ArbExecution especial.
    """
    import time as _time
    from ...core.execution import ArbExecution, TradeExecution

    outcomes   = nr_opp.get("outcomes", [])
    event_id   = nr_opp.get("event_id", "")
    title      = nr_opp.get("event_title", "")
    profit_100 = nr_opp.get("fee_adjusted_profit", 0.0)
    sum_prices = nr_opp.get("total_yes_price", 1.0)
    kelly_usd  = nr_opp.get("kelly_size_usd", max_size)

    size = min(kelly_usd, max_size)
    if size <= 0 or not outcomes:
        return None

    # Deduplicación estricta por event_id (guardado como market_id en arb_executions)
    try:
        db = getattr(executor, "db", None)
        if dedupe_always and db and hasattr(db, "get_last_execution_for_market"):
            last = db.get_last_execution_for_market(event_id)
            if last:
                if hasattr(db, "save_arb_decision"):
                    db.save_arb_decision(
                        opportunity_id=f"{event_id}:{_time.time()}",
                        market_id=event_id,
                        market_title=title,
                        decision="SKIP",
                        reason="already_executed_market",
                        data={
                            "last_execution": last,
                            "fee_adjusted_profit": profit_100,
                        },
                    )
                return None
    except Exception:
        pass

    # Distribuir el capital entre todos los outcomes proporcionalmente
    size_per_outcome = size / len(outcomes)

    # Crear trades simulados para cada outcome
    yes_trade = TradeExecution(
        mode       = "paper",
        market_id  = event_id,
        token_id   = f"negrisk:{event_id}:YES",
        outcome    = "NEGRISK_ALL",
        side       = "BUY",
        size_usd   = size,
        price      = sum_prices / len(outcomes),
        fill_price = sum_prices / len(outcomes),
        fees_usd   = size * 0.002 * len(outcomes),
        success    = True,
        order_id   = f"PAPER-NR-YES-{int(_time.time())}",
    )
    no_trade = TradeExecution(
        mode       = "paper",
        market_id  = event_id,
        token_id   = f"negrisk:{event_id}:PLACEHOLDER",
        outcome    = "NEGRISK_PLACEHOLDER",
        side       = "BUY",
        size_usd   = 0.0,
        price      = 0.0,
        fill_price = 0.0,
        fees_usd   = 0.0,
        success    = True,
        order_id   = f"PAPER-NR-PH-{int(_time.time())}",
    )

    execution = ArbExecution(
        opportunity_id  = f"{event_id}:{_time.time()}",
        market_id       = event_id,
        market_title    = title,
        yes_trade       = yes_trade,
        no_trade        = no_trade,
        spread          = 1.0 - sum_prices,
        net_edge        = profit_100 * 100,
        kelly_fraction  = nr_opp.get("kelly_fraction", 0.0),
        total_invested  = size,
        expected_net_profit = profit_100 * size,
    )

    try:
        # Decision journal (auditabilidad): pre/post ejecución
        try:
            executor.db.save_arb_decision(
                opportunity_id=execution.opportunity_id,
                market_id=event_id,
                market_title=title,
                decision="EXECUTE",
                reason="negrisk_paper",
                data={
                    "fee_adjusted_profit": profit_100,
                    "spread": 1.0 - sum_prices,
                    "total_yes_price": sum_prices,
                    "num_outcomes": len(outcomes),
                    "kelly_usd": kelly_usd,
                    "size_usd": size,
                    "kelly_fraction": nr_opp.get("kelly_fraction", 0.0),
                },
            )
        except Exception:
            pass

        executor.db.save_arb_execution(execution)
        stats["executed"] += 1
        import logging
        logging.getLogger(__name__).info(
            "[PAPER-NegRisk] %s | Outcomes=%d | Edge=%.2f%% | Size=$%.2f | P&L≈$%.4f",
            title[:45], len(outcomes), profit_100 * 100, size, profit_100 * size,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("NegRisk journal error: %s", e)

    return execution


# ─── Journal display ──────────────────────────────────────────────────────────

def _display_journal(console, db, output_format):
    """Muestra el historial del trade journal."""
    try:
        execs   = db.get_recent_executions(limit=50)
        summary = db.get_execution_summary(hours=24)
    except AttributeError:
        console.print("[red]Journal no disponible. Verifica que database.py está actualizado.[/red]")
        return

    if output_format == "json":
        print_json({"summary": summary, "executions": execs})
        return

    from rich.panel import Panel as _Panel
    s = summary
    pnl = s.get("expected_pnl", 0.0) or 0.0
    console.print()
    console.print(_Panel("[bold]Trade Journal — últimas 24h[/bold]", border_style="cyan"))
    console.print(
        f"Arbs:     [cyan]{s.get('total_arbs', 0)}[/cyan]  "
        f"Éxito:    [green]{s.get('successful', 0)}[/green]  "
        f"Capital:  [cyan]${s.get('total_invested', 0) or 0:.2f}[/cyan]  "
        f"P&L est.: [{'green' if pnl >= 0 else 'red'}]${pnl:.4f}[/]  "
        f"Edge med: [yellow]{s.get('avg_edge', 0) or 0:.3f}%[/yellow]"
    )
    console.print()

    if not execs:
        console.print("[dim]Sin ejecuciones registradas. Corre 'polyterm trade --include-negrisk'[/dim]")
        return

    from rich.table import Table as _Table
    t = _Table(box=None, header_style="bold cyan")
    t.add_column("Timestamp",  width=19)
    t.add_column("Mercado",    max_width=38, style="green")
    t.add_column("Modo",       width=5)
    t.add_column("Invertido",  justify="right", width=9)
    t.add_column("Net Edge",   justify="right", width=9)
    t.add_column("P&L Est.",   justify="right", width=10)
    t.add_column("OK",         justify="center", width=4)

    for ex in execs:
        ts  = str(ex.get("timestamp", ""))[:19]
        ok  = ex.get("both_success", 0)
        pnl = ex.get("expected_profit", 0.0) or 0.0
        inv = ex.get("total_invested", 0.0) or 0.0
        edg = ex.get("net_edge", 0.0) or 0.0
        t.add_row(
            ts,
            (ex.get("market_title", "") or "")[:38],
            (ex.get("mode", "") or "")[:5],
            f"${inv:.2f}",
            f"{edg:.3f}%",
            f"[{'green' if pnl >= 0 else 'red'}]${pnl:.4f}[/]",
            "[green]OK[/green]" if ok else "[red]X[/red]",
        )
    from ...utils.paths import get_polyterm_dir
    console.print(t)
    console.print(f"\n[dim]DB: {get_polyterm_dir()}/data.db[/dim]")
