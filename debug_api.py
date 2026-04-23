#!/usr/bin/env python3
"""
Script de diagnóstico — ejecutar desde la raíz del proyecto:
    python debug_api.py
Muestra la estructura real de los datos que devuelve Gamma API
para entender por qué el scanner no encuentra spreads.
"""
import sys, json
sys.path.insert(0, ".")

from polyterm.api.gamma import GammaClient
from polyterm.utils.config import Config

config = Config()
gamma  = GammaClient(base_url=config.gamma_base_url, api_key=config.gamma_api_key)

print("Fetching 3 markets from Gamma API...")
markets = gamma.get_markets(active=True, closed=False, limit=3)

print(f"\nTipo de respuesta: {type(markets)}")
print(f"Número de items: {len(markets) if isinstance(markets, list) else 'N/A'}")

if not markets:
    print("ERROR: La API no devolvió mercados.")
    sys.exit(1)

m = markets[0]
print(f"\n{'='*60}")
print("MERCADO 0 — campos disponibles:")
print(f"{'='*60}")
for k, v in m.items():
    val_str = str(v)[:120]
    print(f"  {k:<30} = {val_str}")

print(f"\n{'='*60}")
print("CAMPOS DE PRECIO (los que usa el scanner):")
print(f"{'='*60}")
for field in ['outcomePrices', 'bestAsk', 'bestBid', 'lastTradePrice',
              'price', 'prices', 'tokens', 'clobTokenIds',
              'outcomes', 'markets', 'question']:
    val = m.get(field)
    if val is not None:
        print(f"  ✅ {field}: {str(val)[:120]}")
    else:
        print(f"  ❌ {field}: (no existe)")

# Mostrar outcomePrices de los 5 primeros mercados
print(f"\n{'='*60}")
print("outcomePrices de los primeros 5 mercados:")
print(f"{'='*60}")
for i, mkt in enumerate(markets[:5]):
    q  = mkt.get('question', mkt.get('title', 'sin título'))[:60]
    op = mkt.get('outcomePrices', 'CAMPO_AUSENTE')
    ba = mkt.get('bestAsk', '')
    bb = mkt.get('bestBid', '')
    print(f"  [{i}] {q}")
    print(f"       outcomePrices={op!r}  bestAsk={ba!r}  bestBid={bb!r}")

gamma.close()
print("\nDiagnóstico completo.")