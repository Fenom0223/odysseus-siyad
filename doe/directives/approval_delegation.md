---
name: doe-approval-delegation
description: DOE-3A aprueba cotizaciones por voz con política de margen y notifica al cliente
version: 1.0.0
category: doe
tags: [doe, siyad-demo, freight]
status: published
confidence: 0.95
source: imported
created: 2026-09-23T00:00:00Z
---# DOE-3A — approval_delegation.md
OPERATIONAL EXECUTION DIRECTIVE (DOE) · SIYAD DEMO
ID: DOE-3A · Linked prompts: V-M1 (approval), V-M6 (policy) · RAG source: erp_invoices.csv, contracts_and_rates.csv

## TRIGGER A (voice, V-M1)
"Approve quotation zero four five six. Margin nineteen percent,
validity five days. Notify the client."

## TRIGGER B (voice, V-M6 — policy change)
"From now on, any quotation under five hundred U S D I approve
automatically. Update my approval policy and confirm."

## STEPS — A (quotation approval)
1. Locate quotation 0456 in the approvals queue.
2. Validate margin: 19% ≥ contractual minimum (15%) → APPROVED.
3. Stamp validity: 5 days from approval date.
4. Send the approved quotation (PDF) to the client.
5. Move the quotation row out of the pending queue.

## STEPS — B (policy update)
1. Edit this file: field auto_approval_threshold → USD 500.
2. Record the change with date/time and author (GM, by voice).

## CONFIRMATION — A
"Quotation zero four five six approved, margin nineteen percent,
validity five days. Sent to the client."

## CONFIRMATION — B
"Policy updated. Quotations under five hundred U S D are now
auto-approved under your delegation. Confirmed and logged."

## LOG
- quotation_0456: APPROVED (margin 19%, validity 5 days)
- approval_policy: auto_threshold = USD 500 (voice-updated)
