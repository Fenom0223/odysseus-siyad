---
name: doe-pricing-navieras
description: DOE-7A cotiza rutas con tarifas de navieras vigentes bajo presión de reunión
version: 1.0.0
category: doe
tags: [doe, siyad-demo, freight]
status: published
confidence: 0.95
source: imported
created: 2026-09-23T00:00:00Z
---# DOE-7A — pricing_navieras.md
OPERATIONAL EXECUTION DIRECTIVE (DOE) · SIYAD DEMO
ID: DOE-7A · Linked prompt: V-E8 · RAG source: contracts_and_rates.csv (CON-011 REEFER)

## TRIGGER (GM voice, at the table with the prospect)
"Quick quote, I am in a meeting: five forty foot reefer
containers, Jebel Ali to Rotterdam, monthly, fresh dates,
temperature controlled at minus two degrees. I need a price
range to answer now."

## INPUTS
- Route: Jebel Ali - Rotterdam · Equipment: REEFER 40HC
- Volume: 5 containers/month · Contract: CON-011 (Al Futtaim Logistics)
- Contracted rate: AED 23,800 per container · Min margin: 15%

## STEPS
1. Read the active reefer rate from contracts_and_rates.csv (CON-011).
2. Compute: 5 × 23,800 = AED 119,000 monthly.
3. Convert to USD (peg 3.6725): ≈ USD 32,400 monthly.
4. Generate the formal quotation PDF (5-day validity, 15% margin).

## CONFIRMATION (expected on-screen output)
"Contracted reefer forty high cube, Jebel Ali to Rotterdam:
twenty-three thousand eight hundred dirhams per container.
For five containers monthly: one hundred nineteen thousand
dirhams — roughly thirty-two thousand four hundred U S D.
Margin per contract at fifteen percent. Want the formal P D F?"

## LOG
- reefer_quotation: 5 × REEFER 40HC = AED 119,000/month (CON-011)
- pdf_generated: quote_reefer_JBARR_5x40HC.pdf
