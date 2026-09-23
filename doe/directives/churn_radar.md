---
name: doe-churn-radar
description: DOE-2A detecta riesgo de churn de cliente clave y propone gestiones de retención desde voz
version: 1.0.0
category: doe
tags: [doe, siyad-demo, freight]
status: published
confidence: 0.95
source: imported
created: 2026-09-23T00:00:00Z
---# DOE-2A — churn_radar.md
OPERATIONAL EXECUTION DIRECTIVE (DOE) · SIYAD DEMO
ID: DOE-2A · Linked prompt: V-M3 · RAG source: erp_customers.csv, rfq_performance.csv

## TRIGGER (GM voice)
"Schedule a dinner with the C E O of Gulf Marine Shipping for
Thursday, and draft a volume discount proposal of five percent
for their next ten imports."

## INPUTS
- Active churn alert: CUS-002 Gulf Marine Shipping
  (rfq_performance: RFQs 4→3, response time 31.5h→46.2h, won 1→0)

## STEPS
1. Create calendar event: dinner with Gulf Marine Shipping CEO,
   next Thursday, 8:30 PM, venue to confirm.
2. Draft volume discount proposal: 5% on the next 10 imports
   (active contract CON-002 as baseline).
3. Log the retention plan in the churn log, linked to alert
   CUS-002 (status: ACTIVE → RETENTION IN PROGRESS).
4. Send the proposal draft to the GM's inbox for review.

## CONFIRMATION (expected on-screen output)
"Dinner scheduled with Gulf Marine C E O for Thursday. Volume
discount proposal drafted: five percent, next ten imports.
Retention plan logged against the churn alert."

## LOG
- event_created: dinner_gulf_marine_thursday
- proposal: vol_discount_5pct_next10imports (draft)
- churn_log: CUS-002 → RETENTION IN PROGRESS
