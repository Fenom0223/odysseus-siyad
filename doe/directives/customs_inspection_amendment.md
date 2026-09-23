---
name: doe-customs-inspection-amendment
description: DOE-11A archiva enmienda aduanera por discrepancia de conteo físico y actualiza memoria compliance
version: 1.0.0
category: doe
tags: [doe, siyad-demo, freight]
status: published
confidence: 0.95
source: imported
created: 2026-09-23T00:00:00Z
---# DOE-11A — customs_inspection_amendment.md
OPERATIONAL EXECUTION DIRECTIVE (DOE) · SIYAD DEMO
ID: DOE-11A · Linked prompts: V-M8 (amendment + rule), V-E12 (inspection close) · RAG source: erp_shipments.csv, compliance_memory.csv (write)

## TRIGGER A (voice, V-M8)
"Container four four seven one eight three seven, shipment zero
eight four seven: physical count four hundred eighty against
declared five hundred. File the customs amendment for twenty
cartons short landed and register a new compliance rule so every
discrepancy over two percent opens an inspection automatically."

## TRIGGER B (voice, V-E12 — close-out)
"Inspection finished: four hundred eighty cartons counted, photos
on file, customs slip signed. Close the inspection and log the
case for the client file."

## STEPS — A (amendment + rule)
1. Locate DXB-2024-0847 (MSKU 4471837) in erp_shipments.csv.
2. File the customs amendment: 480 vs 500 declared
   (20 cartons short landed, seal 44892 intact).
3. Create rule in compliance_memory.csv → R-001:
   IF (count_gap > 2%) THEN open_inspection + notify_broker.
4. Confirm the amendment number and the created rule.

## STEPS — B (inspection close-out)
1. Attach evidence: B6 (bay, 480 cartons counted) +
   B7 (customs slip signed).
2. Close the DXB-2024-0847 inspection case → INSPECTED-480.
3. Log the case in the customer file (Al Naboodah).

## CONFIRMATION — A
"Customs amendment filed for shipment zero eight four seven:
four hundred eighty versus five hundred declared, twenty cartons
short landed. Compliance rule registered: any discrepancy above
two percent now opens an inspection automatically. Confirmed."

## CONFIRMATION — B
"Inspection closed. Four hundred eighty cartons counted, customs
slip signed, photos archived. Case logged to the client file."

## LOG
- amendment: DXB-2024-0847 · 480 vs 500 · FILED
- compliance_rule: R-001 (gap > 2% → automatic inspection)
- inspection: CLOSED · 480 cartons · slip signed · photos B6+B7
