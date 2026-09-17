# Pocket-to-Office — Listener Matrix para Odysseus

Esta carpeta agrega a **Odysseus** la capacidad de recibir tareas delegadas desde
una nota de voz enviada en **Element X** y devolver el resultado (texto +
archivos) a la sala. El usuario lo ve en el móvil.

```
Element X (voz) → np-bot (STT + LLM) → NP_TASK:{...} → @desktop-<user>
                                                              │
                        Odysseus ejecuta (RAG/DOE/servicios) ─┘
                                                              │
Element X (móvil) ← resultado + m.file (informe PDF/CSV) ←────┘
```

## 1. Archivos

| Archivo | Rol |
|---|---|
| `np_office_listener.py` | Listener Matrix (matrix-nio), autocontenido (~230 líneas) |

## 2. Credenciales

Las genera `onboard.sh` (repo `np-sovereign-core`) en el `docker.env` del usuario:

```ini
NP_MX_HOMESERVER=https://np-cpu-<cliente>.<dominio>
NP_MX_DESKTOP_MXID=@desktop-<usuario>:<dominio>
NP_MX_DESKTOP_TOKEN=<token>
NP_MX_OFFICE_ROOM_ID=!abc123:<dominio>
NP_MX_OFFICE_ALIAS="#mi-oficina-<usuario>:<dominio>"
NP_MX_BOT_MXID=@np-bot:<dominio>
```

Odysseus ya usa `docker-compose.yml` con `environment:`/`--env-file`, así que el
mismo archivo sirve para el contenedor de la app y para el listener.

## 3. Wiring — elegir UN camino

### Camino A — HTTP contra el orquestador (recomendado: cero acople)

Odysseus expone FastAPI en el puerto **7000** (`APP_PORT`). Agregar una ruta
(p. ej. junto a las de `routes/`):

```python
# routes/office_routes.py
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/office", tags=["office"])

class OfficeTask(BaseModel):
    task_id: str
    text: str
    locale: str = "es"
    from_: str = ""

@router.post("/task")
async def office_task(task: OfficeTask):
    # 1) Ejecutar el flujo existente de Odysseus con task.text
    #    (services/research, services/docs, mcp_servers/rag_server, memory)
    answer = await run_odysseus_task(task.text)   # <- adaptar al entrypoint real
    files  = []                                    # ej: ["/app/data/informe.pdf"]
    return {"answer": answer, "files": files}
```

Registrar el router en `app.py` (`app.include_router(...)`) y exportar:

```ini
NP_OFFICE_TASK_URL=http://127.0.0.1:7000/office/task
```

### Camino B — Python in-process

```python
from np_office.np_office_listener import OfficeListener, Task, TaskResult

async def handler(task: Task) -> TaskResult:
    answer = await run_odysseus_task(task.text)
    return TaskResult(text=answer, files=[...])

OfficeListener(handler=handler).run_forever()
```

### Opción C — como servicio interno (si preferís la arquitectura de Odysseus)

Mover el listón a `services/office/` y arrancarlo desde el lifespan de `app.py`
como un `asyncio.create_task(listener.run_forever())`. Ventaja: un solo proceso,
mismo ciclo de vida que la app.

## 4. Ejecución

### Docker (sidecar)

```yaml
services:
  odysseus:
    # ...igual que hoy...
    env_file: ./docker.env

  np_office_listener:
    image: <misma imagen de odysseus>
    command: ["python", "np_office/np_office_listener.py"]
    env_file: ./docker.env
    volumes:
      - ${APP_DATA_DIR:-./data}:/app/data:z
    depends_on: [odysseus]
    restart: always
```

### Local (dev)

```bash
pip install "matrix-nio[e2e]"
set -a && . ./docker.env && set +a     # o exportar a mano
python np_office/np_office_listener.py --selfcheck
python np_office/np_office_listener.py
```

### Validación

```bash
python -m py_compile np_office/np_office_listener.py
python np_office/np_office_listener.py --selfcheck
```

Log esperado:

```
[OFFICE] BOOT mxid=@desktop-juan:dominio homeserver=https://np-cpu-... room=!abc:dominio executor=http trusted=@np-bot:dominio
[OFFICE] TASK_RECEIVED task_id=t-001 chars=86
[OFFICE] TASK_DONE task_id=t-001 seconds=9.2 files=1
[OFFICE] FILE_SENT name=informe.pdf bytes=48213
```

## 5. Notas

- **Aislamiento**: `@desktop-<usuario>` solo es miembro de su propia sala; el
  ruteo lo garantiza Synapse, no el código.
- **DOE es estándar**: las directivas `.md` (GEMINI.md) y Playwright como MCP
  viajan en la **imagen base** (código compartido), nunca en el `docker.env`.
- **E2EE (por defecto ON)**: `onboard.sh` crea la sala con `m.room.encryption`
  (megolm) y el listener exige E2EE (`NP_OFFICE_ENCRYPTED=1`). Requiere
  `matrix-nio[e2e]` (libolm) y el **device_id FIJO** de
  `NP_MX_DESKTOP_DEVICE_ID`: la cuenta Olm vive en `NP_OFFICE_STATE_DIR`, así que
  si el device_id cambia entre arranques el listener no descifra. Los archivos
  se suben cifrados (`upload(..., encrypt=True)`). Para sala en claro (debug):
  `NP_OFFICE_E2EE=0 ./onboard.sh <cliente>.env <usuarios>.csv`
- **Idempotencia**: eventos ya procesados descartados (últimos 500); se rechaza
  cualquier remitente distinto de `NP_MX_BOT_MXID`.
