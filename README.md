# PolyTerm

Terminal-based analytics and intelligence layer for Polymarket.

## Quick Start

```bash
pip install -e .
polyterm
```

## Arbitrage Trading Bot

```bash
# Paper trading (no real money)
polyterm trade --bankroll 1000 --min-edge 0.005

# Real trading (requires API credentials)
export POLYMARKET_PRIVATE_KEY="0x..."
export POLYMARKET_API_KEY="..."
export POLYMARKET_API_SECRET="..."
export POLYMARKET_API_PASSPHRASE="..."
polyterm trade --mode real --bankroll 500 --max-size 25
```

## Features

- Real-time market monitoring
- Whale & insider detection
- Arbitrage scanning (intra-market + NegRisk)
- Kelly Criterion position sizing
- Slippage-aware execution
- Paper & real trading modes
- SQLite trade journal (~/.polyterm/data.db)

## More Commands

```bash
polyterm monitor       # Monitor top markets
polyterm whales        # Track whale activity  
polyterm arbitrage     # Scan for arb opportunities
polyterm negrisk       # Multi-outcome arb
polyterm orderbook     # Live order book
polyterm predict       # AI predictions
```
