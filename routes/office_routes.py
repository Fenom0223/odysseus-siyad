"""Pocket-to-Office executor for Odysseus.

Endpoint consumido por el listener Matrix (np_office/np_office_listener.py):
    POST /office/task
    { "task_id", "text", "locale", "from" } -> { "answer": str, "files": [paths] }

Ejecuta el pipeline real de investigacion/respuesta de Odysseus
(src/research_handler.call_research_service) resolviendo el endpoint/modelo
desde la configuracion de la app. El listener corre en el sidecar
`np_office_listener` (docker-compose.yml) y comparte /app/data para los files.

Nota: app.py exime /office del timeout duro y del AuthMiddleware porque el
listener es un contenedor distinto (no loopback) y no manda credencial.
"""

import logging
import re
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


class OfficeTaskRequest(BaseModel):
    task_id: str = ""
    text: str = ""
    locale: str = "es"
    from_: Optional[str] = Field(default=None, alias="from")

    model_config = {"populate_by_name": True}


def _resolve_endpoint():
    """Resolver endpoint/modelo como lo hacen las rutas de research."""
    try:
        from src.endpoint_resolver import resolve_endpoint
    except Exception as exc:  # noqa: BLE001
        logger.warning("office: endpoint_resolver no disponible: %s", exc)
        return "", "", {}

    for prefix in ("research", "default", "utility", "chat"):
        try:
            url, model, headers = resolve_endpoint(prefix, owner=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("office: resolve_endpoint(%s) fallo: %s", prefix, exc)
            continue
        if url and model:
            return url, model, (headers or {})
    return "", "", {}


def _reports_dir() -> Path:
    try:
        from src.constants import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:  # noqa: BLE001
        base = Path("/app/data")
    return base / "office_reports"


def setup_office_routes(research_handler, session_manager=None) -> APIRouter:
    router = APIRouter(tags=["office"])

    @router.post("/office/task")
    async def office_task(body: OfficeTaskRequest) -> dict:
        text = (body.text or "").strip()
        if not text:
            return {"answer": "", "files": []}

        ep_url, ep_model, ep_headers = _resolve_endpoint()
        if not ep_url or not ep_model:
            return {
                "answer": (
                    "Odysseus no tiene un endpoint de modelo configurado. "
                    "Configurar un modelo en Settings antes de delegar tareas."
                ),
                "files": [],
            }

        try:
            answer = await research_handler.call_research_service(
                text,
                ep_url,
                ep_model,
                max_time=900,
                llm_headers=ep_headers,
                max_rounds=20,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("office: research fallo")
            return {
                "answer": f"Odysseus no pudo ejecutar la tarea: {type(exc).__name__}: {exc}",
                "files": [],
            }

        files: List[str] = []
        try:
            reports = _reports_dir()
            reports.mkdir(parents=True, exist_ok=True)
            safe_id = _SAFE_ID.sub("-", (body.task_id or "task"))[:80] or "task"
            report = reports / f"office-{safe_id}.md"
            report.write_text(str(answer or ""), encoding="utf-8")
            files.append(str(report))
        except Exception as exc:  # noqa: BLE001
            logger.warning("office: no pude escribir el reporte: %s", exc)

        logger.info(
            "office: TASK_DONE task_id=%s chars_in=%s files=%s",
            body.task_id, len(text), len(files),
        )
        return {"answer": str(answer), "files": files}

    return router
