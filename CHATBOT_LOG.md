# CHATBOT_LOG.md

Documento vivo para mantener **control auditable** del bot de trading de Polymarket dentro de `polyterm`.

Fecha de última actualización: **2026-04-23**

---

## 1) Qué es este bot

- **Nombre del proyecto**: `polyterm`
- **Tipo**: App **CLI/TUI** en Python con persistencia local (SQLite) y logging a fichero.
- **Propósito**: Capa de analítica/monitorización para Polymarket y un comando de trading (`polyterm trade`) orientado a:
  - **NegRisk (multi-outcome)** como estrategia principal (default **ON**).
  - **Intra-market binario (YES/NO)** como estrategia secundaria.

---

## 2) Entrypoints y comandos relevantes

- **Entrypoint CLI**: `polyterm=polyterm.cli.main:cli` (`setup.py`)
- **Comando trading**: `polyterm trade` (`polyterm/cli/commands/trade.py`)
- **Reporte pre-LIVE**: `scripts/prelive_report.py`

---

## 3) Directorio de runtime (DB, logs, locks, flags)

El sistema usa un “runtime dir” centralizado por `polyterm/utils/paths.py:get_polyterm_dir()`.

- **Orden de resolución** (resumen operativo):
  - `POLYTERM_DIR` (si existe)
  - fallback a un `logs/` local al proyecto (y/o CWD) según implementación

Dentro de ese directorio se guardan:

- **DB**: `data.db`
- **Logs**: `polyterm.log` (configurado en `polyterm/cli/main.py`)
- **Locks**: p.ej. `gamma_rate.lock` (ver `polyterm/api/gamma.py`)
- **Flags**: onboarding / tips / tutorial (varios módulos TUI)

---

## 4) Estrategias implementadas (resumen)

### 4.1 NegRisk (multi-outcome)

- **Señal**: comprar todos los outcomes YES de un evento cuando \(\sum p_{YES} < 1\) (ajustado por fee).
- **Implementación**: `polyterm/core/negrisk.py` (`NegRiskAnalyzer`)
- **Ejecución en `trade`**:
  - En este comando se usa ejecución tipo “paper basket” (ver `_execute_negrisk_paper()` en `polyterm/cli/commands/trade.py`).

### 4.2 Intra-market binario (YES+NO)

- **Señal**: \(ask_{YES}+ask_{NO} < 1 - min\_spread\)
- **Implementación**: `polyterm/core/arbitrage.py` (`ArbitrageScanner`)
- **Ejecución**: `polyterm/core/execution.py` (`ArbExecutor`)

---

## 5) Modo paper vs real (y seguridad de ejecución)

- **paper**: simula sin riesgo (pero deja journal en DB).
- **real**: envía órdenes reales (requiere creds).

Protección crítica ya aplicada:

- **Bloqueo de modo REAL si token IDs inválidos/placeholder**:
  - `polyterm/core/arbitrage.py` adjunta `yes_token_id/no_token_id` desde `clobTokenIds`.
  - `polyterm/core/execution.py` prioriza esos token ids y **rechaza** placeholders en `ExecutionMode.REAL`.

---

## 6) Observabilidad y auditoría

### 6.1 Logging (fichero)

- Logging a `POLYTERM_DIR/polyterm.log` (config en `polyterm/cli/main.py`).

### 6.2 Journal en SQLite (auditoría)

Tablas clave:

- **`arb_executions`**: ejecución de oportunidades (paper/real) con timestamps y métricas (edge, sizes, expected_profit, etc.).
- **`arb_decisions`**: *trazabilidad* de decisiones del pipeline:
  - `decision`: `SKIP` / `EXECUTE` / `ERROR`
  - `reason`: motivo normalizado (ej.: `edge_below_threshold`, `whale_filter`, `exec_exception`, etc.)
  - `data`: JSON con contexto (parámetros, métricas, ids)

Objetivo:

- Hacer posible un **reporte pre-LIVE** con criterios verificables, sin depender de logs “humanos”.

---

## 7) Evaluación pre-LIVE (gates PASS/FAIL)

- Script: `scripts/prelive_report.py`
- Métricas típicas (según implementación previa):
  - decisiones totales, ejecuciones totales
  - success-rate / error-rate
  - diversidad de mercados (unique markets)
  - concentración (top market share)
- Salida: report con **PASS/FAIL** por gate.

---

## 8) Reglas anti re-ejecución (estado actual)

### 8.1 Política vigente: dedupe estricto por `market_id`

**Regla**: si existe **cualquier** ejecución previa en `arb_executions` para un `market_id`, entonces:

- se registra `arb_decisions` con:
  - `decision="SKIP"`
  - `reason="already_executed_market"`
- y **no se ejecuta** de nuevo esa oportunidad, **independientemente del estado del mercado** (cerrado/resuelto/no resuelto).

Aplica tanto a:

- **intra-market** (`_process_opportunity`)
- **NegRisk** (`_execute_negrisk_paper`, usando `event_id` como `market_id` en el journal)

Motivación:

- Evitar loops de re-ejecución por señales repetidas, proteger el dataset pre-LIVE y prevenir comportamiento agresivo/indeseado.

---

## 9) Changelog (cambios relevantes aplicados)

### 2026-04-23 — Observabilidad + preparación pre-LIVE

- **Persistencia centralizada en `POLYTERM_DIR`**:
  - DB, logs, locks y flags migrados desde `~/.polyterm` a un directorio configurable (`polyterm/utils/paths.py`).
- **Auditoría de decisiones**:
  - añadida tabla `arb_decisions` + `save_arb_decision()` para registrar `SKIP/EXECUTE/ERROR` con razones y datos.
- **NegRisk como estrategia primaria**:
  - `--negrisk/--no-negrisk` con default **True**.
- **Modo REAL más seguro**:
  - bloqueo explícito si token ids inválidos/placeholder.
- **Reporte pre-LIVE con gates**:
  - `scripts/prelive_report.py` con PASS/FAIL por umbrales.

### 2026-04-23 — Anti re-ejecución

- **Cambio de cooldown a dedupe estricto**:
  - eliminado `--cooldown-minutes`.
  - eliminada cualquier excepción por mercado cerrado/resuelto.
  - nueva razón de skip: `already_executed_market`.

---

## 10) Operación recomendada (pre-LIVE)

- Ejecutar `polyterm trade --once --format json` para generar decisiones y (si aplica) ejecuciones.
- Ejecutar `python scripts/prelive_report.py` para revisar gates.
- Revisar `POLYTERM_DIR/polyterm.log` ante fallos o skips masivos (p.ej. whale filter o edge thresholds).

