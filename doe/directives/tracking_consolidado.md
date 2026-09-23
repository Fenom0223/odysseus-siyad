---
name: doe-tracking-consolidado
description: DOE-5A actualiza shipment con nueva ETA desde el coche y notifica al cliente
version: 1.0.0
category: doe
tags: [doe, siyad-demo, freight]
status: published
confidence: 0.95
source: imported
created: 2026-09-23T00:00:00Z
---# DOE-5A — tracking_consolidado.md
OPERATIONAL EXECUTION DIRECTIVE (DOE) · SIYAD DEMO
ID: DOE-5A · Linked prompt: V-E5 · RAG source: erp_shipments.csv (write), erp_customers.csv

## TRIGGER (GM voice, hands-free while driving)
"Update shipment zero eight four seven. The container left Jebel
Ali at two P M. Trucking is delayed three hours by traffic on
Sheikh Zayed Road. New E T A at the client warehouse: seven P M
instead of four P M. Notify the client. The driver is Ahmed."

## INPUTS
- shipment_id: DXB-2024-0847 (MSKU 4471837)
- customer: Al Naboodah Group · driver: Ahmed

## STEPS
1. Update erp_shipments.csv row DXB-2024-0847:
   status → IN TRANSIT (trucking), eta_date → 2026-09-22 19:00.
2. Assign driver: Ahmed.
3. Draft and send customer notice (Al Naboodah): new ETA 7 PM,
   reason: traffic on Sheikh Zayed Road.
4. Refresh the tracking tile (desktop) → DELAYED - NEW ETA 7 PM.

## CONFIRMATION (expected on-screen output)
"Done. Shipment zero eight four seven updated. Client notified
with new E T A seven P M. Driver Ahmed assigned."

## LOG
- shipment_update: 0847 IN TRANSIT · ETA 19:00 (was 16:00)
- driver_assigned: Ahmed
- customer_notice: sent (Al Naboodah)
