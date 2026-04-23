# 1. Resumen ejecutivo

- **Qué resuelve el proyecto**: `polyterm` es una app de **monitorización + analítica + utilidades operativas** para Polymarket (prediction markets), con un **bot de “arbitraje”** (principalmente NegRisk multi‑outcome y, secundariamente, intra‑market binario) ejecutable vía CLI/TUI.
- **Qué tipo de sistema es**: sistema **CLI/TUI** en Python, con:
  - clientes HTTP/WS hacia APIs públicas de Polymarket,
  - persistencia local en SQLite (`POLYTERM_DIR/data.db`),
  - comandos operativos (monitor, whales, arbitrage, trade, orderbook, etc.),
  - modo paper y un modo real (parcial/condicional).
- **Flujo de alto nivel (5–10 bullets)**:
  - Entrypoint consola `polyterm=polyterm.cli.main:cli` (`setup.py`).
  - Inicializa config `Config()` (`polyterm/utils/config.py`) y logging a `POLYTERM_DIR/polyterm.log` (resuelto por `polyterm/utils/paths.py:get_polyterm_dir()`) (`polyterm/cli/main.py`).
  - Si se lanza sin subcomando, abre TUI `TUIController.run()` (`polyterm/tui/controller.py`) y despacha pantallas.
  - Si se lanza un subcomando, `LazyGroup` importa el comando bajo demanda (`polyterm/cli/lazy_group.py`).
  - Comandos core consumen datos de `GammaClient` (`polyterm/api/gamma.py`), `CLOBClient` (`polyterm/api/clob.py`) y/o `DataAPIClient` (`polyterm/api/data_api.py`).
  - El bot `polyterm trade` (`polyterm/cli/commands/trade.py`) ejecuta: fetch mercados → scan oportunidades → filtros → sizing → ejecución paper/real → journal en SQLite.
  - Persistencia: `Database` (`polyterm/db/database.py`) mantiene tablas de trades, alerts, snapshots, ejecuciones de arb, decisiones, etc.
  - Observabilidad: logs a fichero + tablas “journal” (ejecuciones y decisiones) + export/report scripts (`scripts/prelive_report.py`).
- **Componentes críticos para operar (trading)**:
  - `polyterm/cli/commands/trade.py` (orquestación).
  - `polyterm/core/negrisk.py` (detección NegRisk).
  - `polyterm/core/arbitrage.py` (scan intra‑market + sizing/slippage).
  - `polyterm/core/execution.py` (ejecución paper/real + journal).
  - `polyterm/db/database.py` (persistencia: `arb_executions`, `arb_decisions`).
  - `polyterm/api/gamma.py`, `polyterm/api/clob.py` (datos y microestructura).

# 2. Objetivo funcional del sistema

- **Qué hace exactamente**:
  - Proporciona herramientas de **observación y análisis** de mercados Polymarket (TUI y CLI).
  - Mantiene una base local de datos (SQLite) con trades/alerts/snapshots/resolutions/ejecuciones.
  - Ejecuta un bot de “arbitraje” en `polyterm trade`:
    - **NegRisk** (multi‑outcome): compra todos los outcomes YES si \(\sum p_{YES} < 1\) ajustado por fee.
    - **Intra‑market binario**: intenta detectar \(ask_{YES}+ask_{NO}<1\) usando CLOB/WS (raro).
- **Entradas**:
  - APIs públicas Polymarket:
    - Gamma REST (`polyterm/api/gamma.py`).
    - CLOB REST/WS (`polyterm/api/clob.py`).
    - Data API (`polyterm/api/data_api.py`), y en algunos puntos acceso directo vía `requests` (p.ej. `polyterm/core/whale_filter.py`).
  - Config local: `POLYTERM_DIR/config.toml` (resuelto por `polyterm/utils/paths.py:get_polyterm_dir()`) (`polyterm/utils/config.py`).
  - Variables de entorno para modo real (ver `polyterm/cli/commands/trade.py` y `polyterm/core/execution.py`).
- **Salidas**:
  - Output en consola (tablas Rich o JSON) en CLI/TUI.
  - Persistencia en `POLYTERM_DIR/data.db` (SQLite).
  - Logging a `POLYTERM_DIR/polyterm.log`.
- **Decisiones que toma**:
  - Selección de oportunidades (thresholds: spread/edge, liquidez, ejecutabilidad).
  - Filtros de riesgo (ballenas/insider) (`polyterm/core/whale_filter.py`).
  - Sizing (Kelly parcial con caps).
  - Ejecución paper/real (con validaciones).
- **Core negocio vs soporte**:
  - **Core trading**: `polyterm/cli/commands/trade.py`, `polyterm/core/negrisk.py`, `polyterm/core/arbitrage.py`, `polyterm/core/execution.py`, `polyterm/db/database.py`.
  - **Soporte/analytics**: `polyterm/core/scanner.py`, `polyterm/core/analytics.py`, `polyterm/core/whale_tracker.py`, `polyterm/core/orderbook.py`, pantallas TUI `polyterm/tui/screens/*`.

# 3. Árbol del repositorio

Árbol resumido (rutas reales):

```text
.
├─ polyterm/                         # Paquete principal
│  ├─ cli/
│  │  ├─ main.py                     # Entrypoint Click + logging + TUI default
│  │  ├─ lazy_group.py               # Carga lazy de subcomandos (LAZY_COMMANDS)
│  │  └─ commands/                   # Subcomandos CLI (trade, whales, arbitrage, etc.)
│  ├─ tui/
│  │  ├─ controller.py               # Loop principal TUI + dispatch de pantallas
│  │  ├─ menu.py / shortcuts.py ...  # UI/UX TUI
│  │  └─ screens/                    # Pantallas (muchas) que invocan core/api/db
│  ├─ core/                          # Lógica core (estrategia, análisis, tracking)
│  │  ├─ negrisk.py                  # Estrategia NegRisk (multi-outcome arb)
│  │  ├─ arbitrage.py                # Scan intra-market/correlated + sizing/slippage
│  │  ├─ execution.py                # Ejecución paper/real + persistencia journal
│  │  ├─ whale_filter.py             # Filtro de riesgo por whale/insider
│  │  ├─ whale_tracker.py            # Tracking de wallets vía WS/REST fallback
│  │  ├─ orderbook.py                # LiveOrderBook + OrderBookAnalyzer + WS feed
│  │  └─ ...                         # Múltiples módulos de analytics/alerts/etc.
│  ├─ api/                           # Adaptadores a APIs externas
│  │  ├─ gamma.py                    # Gamma REST client + rate limiting
│  │  ├─ clob.py                     # CLOB REST + WS (trades y orderbook)
│  │  ├─ data_api.py                 # Data API client (wallet activity/positions)
│  │  └─ aggregator.py / subgraph.py # Agregación + TheGraph (según comando)
│  ├─ db/
│  │  ├─ database.py                 # SQLite schema + operaciones + journals
│  │  └─ models.py                   # Dataclasses: Wallet, Trade, Alert, etc.
│  └─ utils/
│     ├─ config.py                   # Config TOML (defaults + load/save)
│     ├─ errors.py                   # Errores user-friendly (Rich panels)
│     └─ json_output.py              # Contratos JSON/serialización
├─ scripts/
│  ├─ prelive_report.py              # Reporte pre-LIVE + gates PASS/FAIL
│  └─ validate_docs.py               # Utilidad (DESCONOCIDO detalle: ver código)
├─ tests/                            # Suite de tests (core/api/db/cli/tui)
├─ README.md                         # Quick start + overview
├─ requirements.txt                  # Dependencias runtime
├─ setup.py                          # Entry point: "polyterm"
└─ debug_api.py / export2xslx.py     # Utilidades sueltas (posible legacy)
```

Marcado:
- **Entrypoints**: `polyterm/cli/main.py`, `polyterm/tui/controller.py`, `polyterm/cli/commands/*.py`, `scripts/prelive_report.py`.
- **Estrategia**: `polyterm/core/negrisk.py`, `polyterm/core/arbitrage.py`, `polyterm/core/execution.py`, `polyterm/cli/commands/trade.py`.
- **APIs/adapters**: `polyterm/api/gamma.py`, `polyterm/api/clob.py`, `polyterm/api/data_api.py`.
- **Config**: `polyterm/utils/config.py` (TOML).
- **DB/persistencia**: `polyterm/db/database.py`, `polyterm/db/models.py`.
- **Logs/monitorización**: `polyterm/cli/main.py` (logging), `polyterm/core/*` (logger), `scripts/prelive_report.py`.
- **Tests**: `tests/test_core/*`, `tests/test_api/*`, `tests/test_db/*`, `tests/test_cli/*`, `tests/test_tui/*`.

# 4. Componentes principales

## `polyterm/cli/main.py`
- **Responsabilidad**: entrypoint Click `cli()`, crea `Config()`, configura logging a `POLYTERM_DIR/polyterm.log`, lanza TUI si no hay subcomando.
- **Quién lo llama**: console script `polyterm` (`setup.py`).
- **Dependencias**: `click`, `logging`, `Path.home()`, `polyterm/utils/config.py`, `polyterm/tui/controller.py`.
- **Estado que modifica**: escribe en `POLYTERM_DIR/` (p.ej. `polyterm.log`, `data.db`), resuelto por `polyterm/utils/paths.py:get_polyterm_dir()`.
- **Side effects**: filesystem (dir/log), output consola.
- **Fallos**: permisos FS, path HOME inesperado, handlers duplicados (mitigado con `root.handlers.clear()`).

## `polyterm/cli/lazy_group.py`
- **Responsabilidad**: mapa `LAZY_COMMANDS` y carga lazy de módulos en `polyterm/cli/commands/*`.
- **Quién lo llama**: `polyterm/cli/main.py` al construir el grupo Click.
- **Fallos**: import errors por dependencias faltantes; docstrings para help usan AST sobre archivo → puede fallar con sintaxis/encoding.

## `polyterm/tui/controller.py`
- **Responsabilidad**: bucle principal TUI, onboarding en `POLYTERM_DIR/.onboarded`, dispatch de pantallas vía `SCREEN_ROUTES`.
- **Quién lo llama**: `cli()` cuando no hay subcomando.
- **Side effects**: FS (onboard flag), output interactivo, llamadas a APIs/DB indirectamente vía pantallas.
- **Fallos**: excepciones en pantallas se encapsulan con `handle_api_error()` (pero no garantiza trazabilidad).

## `polyterm/cli/commands/trade.py`
- **Responsabilidad**: orquestación del bot de arbitraje.
  - Inicializa `GammaClient`, `CLOBClient`, `DataAPIClient`, `Database`, `OrderBookAnalyzer`, `ArbitrageScanner`, `ArbExecutor`, `WhaleFilter`, `NegRiskAnalyzer`.
  - Loop: fetch markets → `scanner.scan_intra_market_arbitrage()` → (opcional) `negrisk.scan_all()` → filtros → ejecución.
  - Persistencia: `arb_executions` (y `arb_decisions` para auditoría).
- **Entradas**: flags CLI (`--mode`, `--min-edge`, `--min-spread`, `--limit`, `--negrisk/--no-negrisk`, etc.).
- **Outputs**: consola (table/json), DB (`POLYTERM_DIR/data.db`).
- **Side effects**: red (Gamma/CLOB/Data API), DB writes, logs.
- **Fallos típicos**: rate limits, timeouts, JSON malformado, WS no disponible (según otros comandos), decisiones no trazadas si falta DB o falla commit.

## `polyterm/core/negrisk.py` (`NegRiskAnalyzer`)
- **Responsabilidad**: detectar eventos multi‑outcome (>=3 markets) y evaluar si \(\sum p_{YES} < 1\) con ajuste de fee y liquidez.
- **Dependencias**: `GammaClient.get_markets()`, opcional `CLOBClient.get_order_book()` para ask-liquidity; `safe_float()`.
- **Estado**: cache de liquidez `_liquidity_cache` con TTL.
- **Outputs**: dict de oportunidad con `fee_adjusted_profit`, `kelly_fraction`, `kelly_size_usd`, `outcomes[]` (incluye `token_id`).
- **Errores/fallos**:
  - agrupación de eventos puede fallar si payload Gamma cambia (`_extract_event_reference()`).
  - liquidez CLOB: fallos de red → fallback a campos Gamma/vol.
  - sanity-check de sumas `[0.3, 1.5]` puede filtrar eventos válidos si mercados near-resolve o datos stale (riesgo lógico).

## `polyterm/core/arbitrage.py` (`ArbitrageScanner`)
- **Responsabilidad**:
  - `scan_intra_market_arbitrage()`: detecta \(ask_{YES}+ask_{NO} < 1 - min_spread\) con fuentes: WS live prices → CLOB REST → fallback Gamma.
  - Slippage: `_estimate_slippage_from_ob()`.
  - Sizing: `_calculate_kelly()`.
  - Adjunta token IDs (`yes_token_id/no_token_id`) desde `clobTokenIds`.
- **Side effects**: guarda oportunidades en DB vía `_store_opportunity()` (best-effort).
- **Fallos**:
  - Intra-market binario es estructuralmente raro; puede producir cero trades sin que sea bug.
  - Fallback Gamma en binarios puede ser engañoso (la propia docstring lo reconoce).

## `polyterm/core/execution.py` (`ArbExecutor`)
- **Responsabilidad**:
  - `execute(opp)`: decide size (Kelly capped) y ejecuta paper o real.
  - Paper: `_paper_execute()` genera `TradeExecution` YES/NO con slippage simulado.
  - Real: `_real_execute()` usa `py-clob-client` (opcional) y `_place_order()` en executor.
  - Persistencia: `Database.save_arb_execution()` (best-effort) + resumen `get_summary()`.
- **Riesgo crítico**:
  - Modo REAL exige token IDs reales; `_get_tokens()` ahora rechaza placeholders. Sin token ids validos → **bloquea** con excepción explícita.
- **Fallos**: credenciales ausentes, fallos API, órdenes parciales, sincronización temporal (gather), falta de fills reales (fill_price = ref_price).

## `polyterm/core/whale_filter.py` (`WhaleFilter`)
- **Responsabilidad**: bloquear ejecución si detecta ballena/insider reciente por mercado.
- **Integración**: hace `requests.get("https://data-api.polymarket.com/trades", params={"market": market_id})` directamente.
- **Riesgos**: heurísticas de scoring + dependencia de campos variables en Data API; puede filtrar demasiado o no filtrar.

## `polyterm/core/whale_tracker.py` (`WhaleTracker`)
- **Responsabilidad**: monitorizar trades vía WS RTDS (CLOB) y persistir en DB (`trades`, `wallets`, `alerts`) con fallback REST polling.
- **Fallos**: WS reconnections; deduplicación en polling solo por `tx_hash` y “clear set” cuando crece; riesgo de duplicados si hash ausente.

## `polyterm/db/database.py` (`Database`)
- **Responsabilidad**: schema + operaciones SQLite.
- **Tablas relevantes**:
  - `arb_executions` (ejecuciones de arbitraje).
  - `arb_decisions` (decisiones/skip/error del pipeline).
  - `trades`, `wallets`, `alerts`, `market_snapshots`, `positions`, `resolutions`, etc.
- **Side effects**: FS (`POLYTERM_DIR/data.db`), auto cleanup.
- **Fallos**: locks SQLite si procesos concurrentes; migraciones parciales; dependencia de integridad FK (trades → wallets).

# 5. Entry points y ciclo de ejecución

## Entrypoints reales
- **CLI principal**: `polyterm.cli.main:cli` (`polyterm/cli/main.py`).
- **TUI**: `polyterm.tui.controller.TUIController.run()` (`polyterm/tui/controller.py`) se invoca si `cli()` no recibe subcomando.
- **Subcomandos**: cargados por `polyterm/cli/lazy_group.py` desde `polyterm/cli/commands/*.py`.
- **Scripts operativos**:
  - `scripts/prelive_report.py` (reporte + gates).

## Ciclo `polyterm trade`
- `trade()` (`polyterm/cli/commands/trade.py`):
  - Construye clientes `GammaClient`, `CLOBClient`, `DataAPIClient`.
  - Construye `Database`.
  - Construye `OrderBookAnalyzer` (no necesariamente inicia WS).
  - Construye `ArbitrageScanner` (intra-market) y `NegRiskAnalyzer` (multi-outcome).
  - Construye `ArbExecutor` (paper/real).
  - Loop principal:
    - `gamma_client.get_markets(active=True, closed=False, limit=limit)`.
    - `scanner.scan_intra_market_arbitrage(markets)` y filtrado por `min_edge`.
    - `negrisk.scan_all(min_spread=min_spread)` si `--negrisk` (default True).
    - Por oportunidad: `_process_opportunity()` aplica filtro edge, `WhaleFilter` (si activo), y ejecuta `executor.execute()`. Persiste `arb_decisions` y `arb_executions`.
    - Para NegRisk: `_execute_negrisk_paper()` (paper-only en este comando) y journal.
  - Termina por `--once` o Ctrl+C.

**DESCONOCIDO**: si existe un daemon/scheduler formal para ejecutar en background. El repo muestra loops (`while True`) en comandos y en `MarketScanner.start_monitoring()`, pero no un scheduler dedicado.

# 6. Lógica de estrategia (más importante)

## Estrategia principal observada (por `polyterm trade`)

### 6.1 NegRisk (multi‑outcome) — `polyterm/core/negrisk.py`

- **Señal**: para un evento con \(N\) outcomes mutuamente excluyentes:
  - calcula `total_yes = sum(yes_price_i)` a partir de `outcomePrices[0]` de cada market.
  - si `total_yes < 1.0` → “underpriced”: comprar todos los YES.
- **Datos usados**:
  - Gamma `/markets` payload via `GammaClient.get_markets()` (`polyterm/api/gamma.py`).
  - Liquidez ask por outcome:
    - preferente: `CLOBClient.get_order_book(token_id)` suma niveles ask (`polyterm/core/negrisk.py::_get_ask_liquidity`).
    - fallback: campos Gamma `liquidityNum/liquidity/liquidityClob` o proxy `volume24hr * 0.1`.
- **Filtros/validaciones**:
  - Sanity-check: descarta si `raw_sum > 1.5` o `raw_sum < 0.3`.
  - Liquidez: `is_executable` solo si **todos** outcomes superan `min_outcome_liquidity` (default `50.0` USD).
  - Tipo: en `scan_all()` devuelve solo `underpriced` (no implementa short “overpriced”).
- **Edge calculation**:
  - `fee_on_winning = polymarket_fee * (1 - cheapest_outcome_price)`; `polymarket_fee` default 0.02.
  - `net_profit = (1 - total_yes) - fee_on_winning`.
  - expone `fee_adjusted_profit` y `profit_per_100`.
- **Sizing**:
  - `kelly_frac, kelly_usd = _kelly_negrisk(net_profit_fraction=net_profit, num_outcomes=N)`.
  - cap `max_kelly_fraction` (default 0.10).
  - `kelly_usd` capeado por `min_liquidity_usd` (mínimo entre outcomes).

### 6.2 Intra‑market binario — `polyterm/core/arbitrage.py`

- **Señal**: \(ask_{YES} + ask_{NO} < 1 - min_spread\).
- **Precios** (orden de preferencia):
  - live WS (vía `OrderBookAnalyzer.get_live_prices()` si se alimentó WS),
  - CLOB REST por token `CLOBClient.get_order_book(token_id)` para YES y NO,
  - fallback Gamma `outcomePrices` + `bestAsk` (reconocido como aproximación).
- **Slippage**:
  - heurístico por `liquidity` si no hay orderbook,
  - o “consumo” del libro si `OrderBookAnalyzer` disponible.
- **Edge neto**:
  - `net_profit = spread - fee_on_winning` y luego `net_after_slip = (net_profit) - slippage`.
- **Sizing**: `_calculate_kelly(net_after_slip)` → half‑kelly con cap `max_kelly_fraction`.
- **Ejecución**: `ArbExecutor.execute()` compra YES y NO (paper/real).

### 6.3 Filtros que bloquean entrada
- **Edge mínimo**: `min_edge` en `trade.py` se aplica tanto a intra‑market (`opp.net_profit`) como al `fee_adjusted_profit` de NegRisk antes de ejecutar.
- **WhaleFilter**: bloquea si detecta trade grande reciente (`polyterm/core/whale_filter.py`).
- **NegRisk ejecutabilidad**: `is_executable` exige outcomes líquidos (`polyterm/core/negrisk.py`).

### 6.4 Salida/cierre
- Para estas estrategias, “salida” está implícita en la **resolución del mercado** (contrato binario/multi-outcome). No existe un módulo explícito de “close position” para el bot de arb; el bot registra “expected profit”, no PnL realizado.
  - **HIPÓTESIS**: el sistema asume que el arb es “risk‑free hold‑to‑resolution” (conceptualmente válido si el set es completo y ejecutado íntegramente).
  - **DESCONOCIDO**: reconciliación con fills reales en modo REAL y/o monitoreo de resoluciones para PnL realizado.

## Pseudocódigo (estrategia principal: `polyterm trade`)

```pseudo
trade():
  init GammaClient, CLOBClient, DataAPIClient, Database
  init ArbitrageScanner, NegRiskAnalyzer, ArbExecutor, WhaleFilter (optional)

  loop:
    markets = gamma.get_markets(active=true, closed=false, limit=limit)

    intra_opps = scanner.scan_intra_market_arbitrage(markets)
    intra_opps = filter(intra_opps, opp.net_profit >= min_edge*100)

    negrisk_opps = []
    if negrisk_enabled:
      negrisk_opps = negrisk.scan_all(min_spread=min_spread)

    for opp in intra_opps:
      net_edge = opp.net_edge_after_slippage or opp.net_profit
      if net_edge < min_edge*100: journal_decision(SKIP, edge_below_threshold); continue
      if whale_filter and whale_filter.should_skip(opp.market1_id): journal_decision(SKIP, whale_filter); continue
      journal_decision(EXECUTE, pre_execute)
      try:
        execution = executor.execute(opp)
        journal_decision(EXECUTE, post_execute_*)
      except:
        journal_decision(ERROR, exec_exception)

    for nr in negrisk_opps:
      if nr.fee_adjusted_profit >= min_edge and nr.is_executable and not debug:
        execution = execute_negrisk_paper(nr)   # paper basket buy
        journal_decision(EXECUTE, negrisk_paper)

    output table/json
    if once: break
    sleep(scan_interval)
```

## Variables que afectan materialmente el comportamiento

Desde `polyterm/cli/commands/trade.py`:
- `--min-edge` (`min_edge`): umbral de ejecución (edge neto).
- `--min-spread` (`min_spread`): umbral de scan.
- `--limit` (n mercados a escanear).
- `--max-size` (`max_size`): cap por oportunidad.
- `--bankroll`: base para Kelly.
- `--negrisk/--no-negrisk`: activa/desactiva la estrategia dominante.
- `--no-whale-filter`, `--whale-min-usd`, `lookback_minutes` (en WhaleFilter).
- `ExecutionMode` (`paper` vs `real`).

### Control anti re-ejecución (estado actual)
- **Deduplicación estricta por `market_id`**: si existe **cualquier** fila previa en `arb_executions` para ese `market_id`, la oportunidad se marca `SKIP` en `arb_decisions` con:
  - `reason="already_executed_market"`
  - y **no se vuelve a ejecutar**, independientemente de si el mercado está cerrado/resuelto o no.

Desde `polyterm/core/negrisk.py`:
- `polymarket_fee`, `min_outcome_liquidity`, `max_kelly_fraction`, sanity range `[0.3, 1.5]`.

Desde `polyterm/core/arbitrage.py`:
- `min_spread`, `polymarket_fee`, heurística de slippage por `liquidity`.

## Condiciones de entrada/salida (bullets)

- **Entrada NegRisk**:
  - `len(outcomes) >= 3` AND `total_yes < 1.0` AND `spread >= min_spread` AND `is_executable == True`.
- **Entrada intra‑market**:
  - `yes_ask + no_ask < 1 - min_spread` AND `net_edge_after_slippage > 0` AND `net_edge >= min_edge*100` AND `WhaleFilter.allow()`.
- **Salida**:
  - no hay “exit” explícito para el bot; se asume resolución.

## Riesgos lógicos (conceptuales / inconsistencias)

- **NegRisk “completitud”**: la lógica asume que los outcomes forman un set mutuamente excluyente y completo. Hay sanity-check, pero no verificación fuerte de “complete set” ni de que Gamma entregue todos los outcomes relevantes.
- **Ejecución parcial**:
  - En REAL: riesgo de fills parciales por outcome; no hay lógica de rollback/hedge si faltan outcomes.
  - En PAPER: `_execute_negrisk_paper()` registra un “no_trade placeholder”, lo que puede confundir análisis si se interpreta como un par YES/NO estándar.
- **Fees**: fee on winning se aproxima con `cheapest`; es conservador pero depende del modelo de fee real. Si Polymarket fee difiere por mercado/condiciones, el edge está sesgado.
- **Slippage**: intra‑market usa heurísticas cuando no hay orderbook; esto puede flippear decisiones.
- **WhaleFilter**: usa heurísticas y Data API fields variables → alto riesgo de falsos positivos/negativos.

# 7. Métodos y funciones clave

Lista priorizada (firma aproximada + archivo):

- **`cli(ctx)`** — `polyterm/cli/main.py`
  - propósito: entrypoint, config + logging + TUI.
- **`TUIController.run(self)`** — `polyterm/tui/controller.py`
  - propósito: loop interactivo y dispatch.
- **`trade(ctx, mode, bankroll, ...)`** — `polyterm/cli/commands/trade.py`
  - propósito: orquestación del bot de arb.
- **`_process_opportunity(opp, executor, whale_filter, min_edge, stats)`** — `polyterm/cli/commands/trade.py`
  - propósito: filtro→ejecución→journal de decisiones.
- **`NegRiskAnalyzer.scan_all(self, min_spread=0.02, only_executable=True)`** — `polyterm/core/negrisk.py`
  - propósito: produce lista de oportunidades NegRisk.
- **`NegRiskAnalyzer.analyze_event(self, event)`** — `polyterm/core/negrisk.py`
  - propósito: calcula sumas, liquidez, fee-adjusted edge, Kelly.
- **`ArbitrageScanner.scan_intra_market_arbitrage(self, markets)`** — `polyterm/core/arbitrage.py`
  - propósito: oportunidades \(ask_{YES}+ask_{NO}<1\).
- **`ArbExecutor.execute(self, opp)`** — `polyterm/core/execution.py`
  - propósito: sizing + ejecutar paper/real + persistencia.
- **`Database.save_arb_execution(self, execution)`** — `polyterm/db/database.py`
  - propósito: journal de ejecuciones en SQLite.
- **`Database.save_arb_decision(...)`** — `polyterm/db/database.py`
  - propósito: journal de decisiones (execute/skip/error).

# 8. Configuración y variables de entorno

## Config TOML
- Archivo: `POLYTERM_DIR/config.toml` (ruta por defecto lógica; resuelto por `polyterm/utils/paths.py:get_polyterm_dir()`) (`polyterm/utils/config.py`).
- Defaults: `Config.DEFAULT_CONFIG` (incluye endpoints, alert thresholds, whale tracking, arbitrage params).
- Parámetros relevantes:
  - `api.gamma_base_url`, `api.clob_rest_endpoint`, `api.subgraph_endpoint`, etc.
  - `arbitrage.min_spread`, `arbitrage.polymarket_fee`.
  - `whale_tracking.*`.

## Variables de entorno (modo REAL)
Referencias:
- `polyterm/cli/commands/trade.py` muestra las variables requeridas.
- `polyterm/core/execution.py::_init_clob_client()` las consume:
  - `POLYMARKET_PRIVATE_KEY` (sensible, crítico).
  - `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE` (sensible).
  - `POLYMARKET_CHAIN_ID` (por defecto 137).

## Sensibles / riesgo
- Private key y API creds: riesgo de pérdida de fondos; no deben aparecer en logs.

## Inconsistencias a revisar
- `polyterm/core/execution.py` usa `py-clob-client` (comentado en `requirements.txt`); modo real depende de instalación externa.
- `Config` menciona `gamma_markets_endpoint="/events"` pero `GammaClient.get_markets()` usa `/markets` (ver `polyterm/api/gamma.py`). Esto puede ser **legacy** o un ajuste no conectado.

# 9. Persistencia y datos

## Base de datos
- Archivo: `POLYTERM_DIR/data.db` (resuelto por `polyterm/utils/paths.py:get_polyterm_dir()`) (`polyterm/db/database.py`).
- Tablas clave para trading:
  - `arb_executions`: ejecuciones (paper/real).
  - `arb_decisions`: decisiones (execute/skip/error) para auditoría.
  - `arbitrage_opportunities`: oportunidades detectadas (best-effort).
  - `trades`, `wallets`, `alerts`: tracking whale/smart money.
  - `market_snapshots`: historial de mercados (para analytics).
  - `resolutions`: estado de resolución (si se llena; depende de comandos).
- Identificadores importantes:
  - `opportunity_id` en `arb_executions` y `arb_decisions` (formato depende del módulo; p.ej. `f"{market_id}:{timestamp}"`).
  - `market_id`/`token_id` (CLOB).
- Datos que pueden perderse si falla el proceso:
  - decisiones de pipeline si no se registran antes del crash (mitigado parcialmente por journal de decisiones).
  - WS messages no persistidos si cae el proceso (depende de tracker).

# 10. Logging, métricas y observabilidad

## Dónde loguea
- `POLYTERM_DIR/polyterm.log` configurado en `polyterm/cli/main.py` (resuelto por `polyterm/utils/paths.py:get_polyterm_dir()`).
- Módulos usan `logging.getLogger(__name__)` (e.g. `polyterm/core/negrisk.py`, `polyterm/core/execution.py`, `polyterm/core/whale_tracker.py`).

## Qué eventos clave quedan trazados
- `trade_start ...` en `polyterm/cli/commands/trade.py`.
- NegRisk logs informativos y warnings sobre outcomes ilíquidos (`polyterm/core/negrisk.py`).
- Ejecuciones paper/real (`polyterm/core/execution.py`).
- WhaleTracker/WS failures (`polyterm/core/whale_tracker.py`).
- Journals en DB: `arb_executions` y `arb_decisions`.

## Qué métricas revisar cuando algo va mal
- En DB:
  - conteos por `arb_decisions.decision` y `arb_decisions.reason`.
  - `arb_executions.both_success`, `expected_profit`, `net_edge`, `total_invested`.
- En logs:
  - warnings de `NegRiskAnalyzer` sobre iliquidez.
  - errores de WS y fallback REST (WhaleTracker).
  - errores de ejecución real (si se habilita).

## Qué logs faltan (para LIVE)
- No hay trazas uniformes para latencias (request start/end), rate-limit, y tiempos por fase.
- No hay “order lifecycle telemetry” (ack, partial fill, cancel, retry) en modo REAL.
- No hay correlación global (trace_id) más allá de `opportunity_id` (parcial).

## Cómo usar los logs para diagnosticar
- **Si no hay operaciones**:
  - Revisar `arb_decisions` con `scripts/prelive_report.py` (ver sección 14).
  - Buscar razones dominantes: `edge_below_threshold`, `whale_filter`, `NegRisk ... ilíquidos` (log).
- **Si NegRisk no aparece**:
  - Ver logs `NegRiskAnalyzer.find_multi_outcome_events()`/`scan_all()` y comprobar payload Gamma.
  - Confirmar que `polyterm trade` corre con `--negrisk` (default True).
- **Si WS falla**:
  - Revisar errores `WhaleTracker WebSocket failed` y si entra en `falling back to REST polling`.
- **Si REAL falla**:
  - `ArbExecutor._get_tokens()` puede lanzar error de tokens inválidos; revisar que `ArbitrageResult.yes_token_id/no_token_id` venga de `clobTokenIds`.

# 11. Integraciones externas

- **Gamma API (REST)**: `polyterm/api/gamma.py`
  - Uso: listar mercados, detalles, precios.
  - Riesgos: rate limit 429, payload changes, timeouts.
- **CLOB API (REST + WS)**: `polyterm/api/clob.py`
  - Uso: order books (`/book`), prices history, trades, websockets RTDS y orderbook.
  - Riesgos: reconexión, mensajes incompletos, endpoints cambiantes.
- **Data API (REST)**: `polyterm/api/data_api.py` y accesos directos en `polyterm/core/whale_filter.py`
  - Uso: posiciones y activity por wallet; trades por mercado.
  - Riesgos: fields inconsistentes; 429/timeout.
- **TheGraph/Subgraph**: `polyterm/api/subgraph.py` (no inspeccionado aquí) → **DESCONOCIDO** alcance exacto sin leer el archivo.
- **py-clob-client**: requerido para ejecución REAL (`polyterm/core/execution.py`).
  - Riesgos: dependencia opcional; credenciales sensibles; semántica de órdenes.

# 12. Modos de operación

- **TUI interactivo**: por defecto `polyterm` sin subcomando (`polyterm/tui/controller.py`).
- **CLI comandos**: `polyterm <command>` via `polyterm/cli/commands/*`.
- **Trading**:
  - `--mode paper`: simula y registra en DB.
  - `--mode real`: requiere creds y `py-clob-client`; ejecuta órdenes reales.
  - `--negrisk/--no-negrisk`: toggles estrategia NegRisk.
- **Monitorización/colección**:
  - `WhaleTracker.start_monitoring()` usa WS y fallback REST (modo “collector” implícito).

# 13. Riesgos y puntos frágiles

- **Asincronía / reconexiones**:
  - `CLOBClient.listen_for_trades()` y `listen_orderbook()` manejan reconexión; riesgo de “permanent failure” y pérdida de cobertura.
- **Idempotencia**:
  - Fallback REST en `WhaleTracker` deduplica por `tx_hash`; si falta hash o se resetea set, puede duplicar.
- **Ejecución parcial en NegRisk**:
  - No hay lógica de neutralización si se ejecuta parte del basket (crítico para LIVE).
- **Cálculo de edge vs realidad**:
  - Fees y slippage son modelos; sin medición de fills reales, el edge puede ser ilusorio.
- **SQLite concurrency**:
  - si se corren múltiples procesos, riesgo de locks y fallos silenciosos (algunos try/except “pass”).
- **Errores silenciosos**:
  - varios `except Exception: pass` (e.g. `_store_opportunity`) pueden ocultar degradación.

# 14. Preparación para live

## Qué está listo
- Pipeline paper end‑to‑end con persistencia de ejecuciones (`arb_executions`) y decisiones (`arb_decisions`).
- Detectores base NegRisk e intra‑market con heurísticas de liquidez.
- Infra de WS para trades/orderbook (usable para observabilidad).

## Qué no está listo
- Ejecución REAL robusta (fills/partial/cancel/retry/reconcile).
- Gestión de riesgo de ejecución parcial (especialmente NegRisk).
- PnL realizado/mark‑to‑market y reconciliación con resoluciones.
- Observabilidad de latencias y errores por fase (solo parcial).

## Controles/validaciones previas recomendadas (antes de LIVE)
- Gate pre‑LIVE automático (ver `scripts/prelive_report.py`):
  - mínimos de decisiones, diversidad, error‑rate, success‑rate.
- Validación de token IDs reales antes de enviar órdenes (ya bloquea placeholders).
- Rate-limit + backoff consistente en todas las rutas (hoy hay duplicación y acceso directo en WhaleFilter).
- Kill switch: detener trading si error rate sube o WS cae y datos se vuelven stale.

## Lista priorizada

### P0 (bloqueantes para live)
- Implementar lifecycle de órdenes REAL: ack/fill status, timeouts, cancel/repost (`polyterm/core/execution.py`).
- Manejo de ejecución parcial en NegRisk: si falta un outcome, hedgear o abortar (no existe).
- Reconciliación post‑trade con estado externo (ordenes abiertas, fills, balances).
- Métricas mínimas operativas: latency por API, error rates, slippage observado vs esperado.

### P1 (muy importantes)
- Consolidar accesos a Data API (evitar `requests` directo en `polyterm/core/whale_filter.py`) para logging y rate-limit uniforme.
- Reducir `except: pass` en persistencia; elevar a warnings con contexto.
- Añadir trazabilidad por `opportunity_id` de extremo a extremo (incluyendo requests).

### P2 (mejoras recomendables)
- Backtest/sim con datasets persistidos (hay pantallas “backtest” pero **DESCONOCIDO** si está conectado a trading).
- Modelos de slippage más realistas alimentados por orderbook depth real.

# 15. Preguntas abiertas

- **Qué define “completitud” en NegRisk** para un evento en Gamma: ¿se garantiza que `get_markets()` retorna todos los outcomes relevantes?
  - Se requiere: ejemplos reales de payload Gamma para eventos NegRisk.
- **Semántica exacta de fees** en Polymarket por mercado y cómo impacta `fee_on_winning`.
  - Se requiere: docs/ejemplos de fee o logs de settlement.
- **Modo REAL**:
  - ¿Cómo obtener balances, órdenes abiertas y fills vía `py-clob-client`?
  - Se requiere: trazas reales de respuestas del CLOB al postear órdenes.
- **Persistencia de resoluciones**:
  - `resolutions` table existe, pero **DESCONOCIDO** qué comando la llena consistentemente.
  - Se requiere: ejecución de comandos de resolución / logs.

# 16. Guía para otra IA

## GUIA_PARA_ANALISIS_CON_LOGS

- **Orden recomendado de lectura**:
  1. `LOGICA_AI.md` (este archivo).
  2. `polyterm/cli/commands/trade.py` (orquestación) y `polyterm/core/negrisk.py` (señal principal).
  3. `polyterm/core/execution.py` (paper/real) y `polyterm/db/database.py` (journal).
  4. Logs en `POLYTERM_DIR/polyterm.log`.
  5. DB `POLYTERM_DIR/data.db` (tablas `arb_decisions`, `arb_executions`).
- **Para fallos de “no ejecuta”**:
  - Mirar `arb_decisions.reason` (edge_below_threshold, whale_filter, exec_exception, negrisk_paper).
  - Cruzar con logs `NegRiskAnalyzer` (liquidez, sanity-check) y con parámetros CLI (`--min-edge`, `--min-spread`, `--no-negrisk`).
- **Para fallos de ejecución REAL**:
  - Revisar `ArbExecutor._get_tokens()` y si `ArbitrageResult.yes_token_id/no_token_id` viene poblado desde `clobTokenIds` (`polyterm/core/arbitrage.py`).
  - Revisar credenciales requeridas y presencia de `py-clob-client`.
- **Contexto mínimo para proponer mejoras sin romper lógica**:
  - Mantener contrato de `arb_executions` y `arb_decisions` (no romper schema).
  - Mantener flags CLI en `trade.py` (compatibilidad).
  - Añadir métricas/eventos sin cambiar semántica de sizing/edge sin justificar con datos.

---

## RESUMEN_OPERATIVO

### 10 archivos más importantes
1. `polyterm/cli/main.py`
2. `polyterm/cli/lazy_group.py`
3. `polyterm/cli/commands/trade.py`
4. `polyterm/core/negrisk.py`
5. `polyterm/core/arbitrage.py`
6. `polyterm/core/execution.py`
7. `polyterm/db/database.py`
8. `polyterm/api/gamma.py`
9. `polyterm/api/clob.py`
10. `scripts/prelive_report.py`

### 10 funciones/métodos más importantes
1. `polyterm.cli.main.cli(ctx)`
2. `polyterm.tui.controller.TUIController.run(self)`
3. `polyterm.cli.commands.trade.trade(...)`
4. `polyterm.cli.commands.trade._process_opportunity(...)`
5. `polyterm.cli.commands.trade._execute_negrisk_paper(...)`
6. `polyterm.core.negrisk.NegRiskAnalyzer.scan_all(...)`
7. `polyterm.core.negrisk.NegRiskAnalyzer.analyze_event(...)`
8. `polyterm.core.arbitrage.ArbitrageScanner.scan_intra_market_arbitrage(...)`
9. `polyterm.core.execution.ArbExecutor.execute(...)`
10. `polyterm.db.database.Database.save_arb_execution(...)` / `save_arb_decision(...)`

### 5 riesgos más serios
1. Ejecución parcial (especialmente NegRisk) sin hedge/rollback en LIVE.
2. Falta de lifecycle real de órdenes (fills/cancel/retry/reconcile) en `ExecutionMode.REAL`.
3. Modelos de fee/slippage pueden producir edges ilusorios sin observabilidad de fills.
4. Dependencia de payloads externos cambiantes (Gamma/CLOB/Data API) + `except: pass` que oculta fallos.
5. Concurrencia SQLite / múltiples procesos → locks y pérdida de journal sin alertas claras.

### 5 mejoras de mayor impacto antes de live
1. Implementar order lifecycle REAL completo + timeouts/cancel/repost + reconciliación.
2. Gestión de riesgo de ejecución parcial para baskets NegRisk (atomicidad práctica).
3. Observabilidad por fase (latencia, error rate, slippage observado) + alarmas mínimas.
4. Unificar acceso a Data API (sin `requests` directo) con rate limiting y logs.
5. Integrar “gates” pre‑LIVE en CI/operación (usar `scripts/prelive_report.py --gate` como bloqueo).

