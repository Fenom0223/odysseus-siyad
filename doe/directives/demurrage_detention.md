---
name: doe-demurrage-detention
description: DOE-9A gestiona free time ante demurrage y redacta comunicación a la naviera
version: 1.0.0
category: doe
tags: [doe, siyad-demo, freight]
status: published
confidence: 0.95
source: imported
created: 2026-09-23T00:00:00Z
---# DOE-9A — demurrage_detention.md
OPERATIONAL EXECUTION DIRECTIVE (DOE) · SIYAD DEMO
ID: DOE-9A · Linked prompt: V-M7 · RAG source: dnd_exposure.csv, erp_shipments.csv

## TRIGGER (GM voice, crisis)
"Container Em es ki you four four seven one eight two one is
held at Jebel Ali. Request a free time extension from the carrier
and draft the email to Maersk now."

## INPUTS
- Container: MSKU 4471821 (DXB-2024-0809, customs documentary check)
- dnd_exposure: 1 day of free time left, USD 950/day combined
- Carrier: Maersk

## STEPS
1. Read dnd_exposure.csv: free time days remaining and cost/day
   for container MSKU 4471821.
2. Draft email to Maersk: request +3 days free time extension,
   citing the customs documentary hold (not the importer's fault).
3. CC the GM. Send.
4. Update dnd_exposure: extension requested (status PENDING).

## CONFIRMATION (expected on-screen output)
"Extension requested from Maersk for container four four seven
one eight two one. Email drafted and sent: three extra days of
free time, one thousand two hundred dirhams exception so far,
storage risk eight hundred dollars per day. Confirmed."

## LOG
- extension_request: MSKU 4471821 → Maersk (+3 days, PENDING)
- email_sent: maersk_extensions@…
- exposure_check: 950 USD/day combined (D+D)
