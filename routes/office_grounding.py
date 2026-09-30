"""Grounding SQL para las tareas office — SQL-NUMERIC-01 / PROMPT-GROUNDING-01.

HISTORIA (29-sep-2026): el executor respondía con el LLM "pelado" y el modelo
contó INV-0810 (status CREDIT HOLD) como OVERDUE → publicó "5 facturas =
56,700 AED" cuando el ground truth por SQL es 4 = 53,900. El retrieval y el
self-check no lo arreglan: LLM no detecta sus propios errores de aritmética.

Qué hace aquí: carga los CSV de RAG-DATA en SQLite EN MEMORIA y calcula el
agregado con SQL real (COUNT/SUM/ORDER BY) según la intención de la pregunta.
El resultado viaja como bloque [GROUNDING SQL] dentro del prompt con la regla
"usa las cifras tal cifra tal cual"; el modelo sólo redacta. Si la pregunta no toca
datos tabulares devuelve "" y el pipeline queda exactamente como estaba.

CSV_DIR (por orden):
  1. $NP_OFFICE_CSV_DIR
  2. <repo>/data/uploads
  3. el data/uploads del fork RAG (multi-inquilino: los CSV viven allí)
"""

import csv
import os
import re
import sqlite3
from pathlib import Path
from typing import List

_CSV_FALLBACK = Path("/persistent/projects/local-rag-multiagents-project/data/uploads")

# (regex de intención, tablas, [(título, SQL), ...])
_SQL_INTENTS = (
    (
        re.compile(
            r"overdue|vencid|debtors?|deudores?|cobran|invoice|factura|dispute|"
            r"disput|capital tied|dso|aging|cuentas? por cobrar", re.I),
        ("erp_invoices",),
        (
            ("estado de facturas (COUNT/SUM por status)",
             "SELECT status, COUNT(*) AS n, "
             "ROUND(SUM(CAST(amount_aed AS REAL)), 2) AS total_aed "
             "FROM erp_invoices GROUP BY status ORDER BY total_aed DESC"),
            ("vencidas ordenadas por importe DESC",
             "SELECT invoice_id, customer, CAST(amount_aed AS REAL) AS amount_aed, "
             "issued_date, due_date, status FROM erp_invoices "
             "WHERE status = 'OVERDUE' ORDER BY amount_aed DESC"),
            ("total de vencidas",
             "SELECT COUNT(*) AS n, ROUND(SUM(CAST(amount_aed AS REAL)), 2) AS total_aed "
             "FROM erp_invoices WHERE status = 'OVERDUE'"),
            ("top deudores con vencidas",
             "SELECT customer, COUNT(*) AS n, "
             "ROUND(SUM(CAST(amount_aed AS REAL)), 2) AS overdue_aed "
             "FROM erp_invoices WHERE status = 'OVERDUE' "
             "GROUP BY customer ORDER BY overdue_aed DESC"),
            ("disputas abiertas",
             "SELECT invoice_id, customer, "
             "CAST(dispute_amount_aed AS REAL) AS disputed_aed, dispute_reason "
             "FROM erp_invoices WHERE CAST(dispute_amount_aed AS REAL) > 0 "
             "ORDER BY disputed_aed DESC"),
        ),
    ),
    (
        re.compile(
            r"shipment|env[ií]os?|otd|on[ -]?time|delivery|entrega|customs|"
            r"aduana|exception|excepci|carrier|transit|coordinator", re.I),
        ("erp_shipments",),
        (
            ("estado de envíos (COUNT por status)",
             "SELECT status, COUNT(*) AS n FROM erp_shipments "
             "GROUP BY status ORDER BY n DESC"),
            ("OTD por transportista",
             "SELECT carrier, COUNT(*) AS n, "
             "SUM(CASE WHEN status = 'ON TIME' THEN 1 ELSE 0 END) AS on_time, "
             "ROUND(100.0 * SUM(CASE WHEN status = 'ON TIME' THEN 1 ELSE 0 END) "
             "/ COUNT(*), 1) AS otd_pct FROM erp_shipments "
             "GROUP BY carrier ORDER BY carrier"),
            ("exposición total en excepciones",
             "SELECT COUNT(*) AS n, "
             "ROUND(SUM(CAST(exception_cost_aed AS REAL)), 2) AS exposure_aed "
             "FROM erp_shipments WHERE exception_detail <> ''"),
            ("excepciones detalladas",
             "SELECT shipment_id, customer, carrier, status, exception_detail, "
             "CAST(exception_cost_aed AS REAL) AS cost_aed FROM erp_shipments "
             "WHERE exception_detail <> '' ORDER BY cost_aed DESC"),
        ),
    ),
)

GROUNDING_RULE = (
    "\n\n[GROUNDING SQL — cifras ya calculadas con SQL sobre los CSV de RAG-DATA]\n"
    "{grounding}\n\n"
    "REGLA: usa esas cifras TAL CUAL (sumas, conteos y orden ya están calculados). "
    "No recalcules, no estimes y no inventes filas que no aparezcan; si falta un dato "
    "dilo explícitamente. Mantén el idioma del usuario y cita los importes sin redondear.\n"
    "[/GROUNDING SQL]"
)


def _uploads_dir() -> Path:
    env = (os.environ.get("NP_OFFICE_CSV_DIR") or "").strip()
    candidates = [Path(env)] if env else []
    candidates += [Path(__file__).resolve().parents[1] / "data" / "uploads", _CSV_FALLBACK]
    for cand in candidates:
        if cand.is_dir() and any(cand.glob("*.csv")):
            return cand
    return candidates[-1]


def _load_csv_table(conn: sqlite3.Connection, stem: str, folder: Path) -> int:
    """Carga <stem>.csv como tabla SQLite. Todas las columnas TEXT: la
    conversión a número la hace cada SQL con CAST, así un importe mal
    formateado no rompe la carga (y el orden por importe sale correcto)."""
    path = folder / f"{stem}.csv"
    if not path.is_file():
        return 0
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return 0
    header = [re.sub(r"\W+", "_", (c or "").strip()) or f"col{i}"
              for i, c in enumerate(rows[0])]
    body = [r for r in rows[1:] if r]
    conn.execute(f'CREATE TABLE "{stem}" ('
                 + ", ".join(f'"{c}" TEXT' for c in header) + ")")
    payload = [(list(r) + [""] * len(header))[:len(header)] for r in body]
    conn.executemany(
        f'INSERT INTO "{stem}" VALUES ({", ".join("?" * len(header))})', payload)
    return len(payload)


def sql_grounding(query: str) -> str:
    """Agregados deterministas (SQL) sobre los CSV relevantes a la pregunta.

    Devuelve '' si la pregunta no toca datos tabulares o si algo falla: el
    grounding es un refuerzo, nunca un punto único de fallo.
    """
    matched = [(rx, tables, stmts) for rx, tables, stmts in _SQL_INTENTS
               if stmts and rx.search(query or "")]
    if not matched:
        return ""
    folder = _uploads_dir()
    try:
        conn = sqlite3.connect(":memory:")
        loaded = {}
        for _, tables, _ in matched:
            for stem in tables:
                if stem not in loaded:
                    loaded[stem] = _load_csv_table(conn, stem, folder)
        if not any(loaded.values()):
            return ""

        blocks: List[str] = []
        for _, _, stmts in matched:
            for title, sql in stmts:
                try:
                    cur = conn.execute(sql)
                    header = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                except sqlite3.Error as exc:
                    continue  # título con tabla ausente: se omite, no rompe
                if not rows:
                    blocks.append(f"-- {title}\n(sin filas)")
                    continue
                lines = ["| " + " | ".join(header) + " |",
                         "|" + "|".join([" --- "] * len(header)) + "|"]
                lines += ["| " + " | ".join("" if v is None else str(v) for v in r)
                          + " |" for r in rows]
                blocks.append(f"-- {title}\n" + "\n".join(lines))
        conn.close()
        if not blocks:
            return ""
        origen = ", ".join(f"{k}.csv ({v} filas)" for k, v in loaded.items())
        return (f"Fuente: {origen}\n" + "\n\n").join(blocks)
    except Exception as exc:  # noqa: BLE001
        return ""


def grounding_prompt(text: str) -> str:
    """Devuelve el prompt con el bloque [GROUNDING SQL] anexado, o el texto
    original si no hay grounding (pregunta no tabular o CSV no disponible)."""
    grounding = sql_grounding(text)
    if not grounding:
        return text
    return text + GROUNDING_RULE.format(grounding=grounding)
