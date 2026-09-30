#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
np_office_listener.py - Pocket-to-Office (SIYAD / New Paradigm)

La app desktop del cliente escucha su sala privada de Matrix ("Mi Oficina") y
ejecuta las tareas que el bot np-bot delega desde una nota de voz de Element X.
El resultado (texto + archivos) vuelve a la misma sala y el usuario lo ve en
el movil, en tiempo real.

IDENTIDAD
    El listener entra a Matrix como @desktop-<usuario>:<dominio>, con las
    credenciales NP_MX_* que onboard.sh escribe en el docker.env del usuario.
    Aislamiento: @desktop-<usuario> solo es miembro de #mi-oficina-<usuario>,
    por lo que Synapse nunca le entrega tareas de otro usuario. El aislamiento
    lo garantiza el servidor, no este codigo.

CONTRATO DE TAREA (lo que el bot postea en la sala)
    Un mensaje de texto con el prefijo NP_TASK: seguido de un JSON:
        NP_TASK:{"task_id":"t-001","from":"@juan:dominio","text":"...","locale":"es"}
    Si la sala es E2EE, el evento llega cifrado (megolm) y nio lo descifra
    antes de disparar el callback.

INTEGRACION (dos caminos, elegir uno)
    A) HTTP (cero acople): exportar NP_OFFICE_TASK_URL apuntando a una ruta de
       tu app que reciba {"task_id","text","locale","from"} y devuelva
       {"answer":"...","files":["/ruta/al/informe.pdf"]}.
    B) Python (in-process): importar OfficeListener y pasarle un handler:
           async def handler(task): ...
               return TaskResult(text="...", files=["/ruta/al/informe.pdf"])
           OfficeListener(handler=handler).run_forever()

USO
    python np_office_listener.py --selfcheck   # valida configuracion y sale
    python np_office_listener.py               # corre el listener (HTTP o echo)

VARIABLES DE ENTORNO (las escribe onboard.sh en el docker.env del usuario)
    NP_MX_HOMESERVER          https://np-cpu-<cliente>.<dominio>
    NP_MX_DESKTOP_MXID        @desktop-<usuario>:<dominio>
    NP_MX_DESKTOP_TOKEN       token de sesion ya emitido (preferido)
    NP_MX_DESKTOP_PASSWORD    fallback si no hay token
    NP_MX_DESKTOP_DEVICE_ID   device_id FIJO (NPOFFICE-<APP>-<NODO>; legacy:
                              NPOFFICEDESKTOP). Si existe
                              <NP_OFFICE_STATE_DIR>/device_id.txt, el valor del
                              store tiene PREFERENCIA sobre esta variable.
    NP_MX_DEVICE_NAME         (opcional) nombre visible del dispositivo
                              (whitelabel). default: "SIYAD Office Desktop".
    NP_MX_OFFICE_ROOM_ID      !abc123:dominio      (preferido)
    NP_MX_OFFICE_ALIAS        #mi-oficina-<usuario>:<dominio>
    NP_MX_BOT_MXID            @np-bot:<dominio>    (unico remitente confiable)
    NP_OFFICE_TASK_URL        (opcional) endpoint HTTP del ejecutor de tu app
    NP_OFFICE_STATE_DIR       (opcional) store de nio/E2EE. default: ./.np_office
    NP_OFFICE_SYNC_TIMEOUT    (opcional) ms de long-poll. default: 30000
    NP_MX_TRUSTED_SENDERS     (opcional) CSV de remitentes extra confiables
    NP_OFFICE_ENCRYPTED       (opcional) 1 = exigir E2EE (falla si falta olm)
    NP_OFFICE_ENV_FILE        (opcional) ruta del env que entrego el deploy:
                               si las credenciales caducan se relee y se
                               reconecta solo (sin reiniciar el proceso).

DINAMICA (multi-inquilino: un deploy/empleado por app, nada del pasado)
    1. Las credenciales se VALIDAN contra el homeserver (whoami) antes de
       confiar en ellas; un token de un deploy anterior dispara
       CREDENTIALS_STALE, releer NP_OFFICE_ENV_FILE y reintentar con backoff
       en lugar de quedarse "conectado" a la nada.
    2. El store Olm local solo se usa si el homeserver CONFIRMA que nuestro
       device tiene claves publicadas. Si el HS es de otro deploy (device
       recien creado, sin claves), nio diria "already published" y nunca
       volveria a subirlas -> sala E2EE muda para siempre. En ese caso se
       regenera la cuenta Olm (E2EE_STORE_RESET) y se publican claves nuevas.
    3. La sala se resuelve SIEMPRE por alias (estable por usuario/deploy);
       el room_id del env solo se usa si no hay alias configurado. Un
       room_id heredado de un deploy anterior no puede secuestrar la sala.

E2EE
    Requiere matrix-nio[e2e] (libolm). El device_id DEBE ser fijo y el store
    (NP_OFFICE_STATE_DIR) persistente: la cuenta Olm vive ahi. Si el device_id
    cambia entre arranques, el listener no puede descifrar la sala.
    Los archivos se suben cifrados (upload(..., encrypt=True)) y viajan como
    "file" con las claves, igual que hace el bot.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

try:
    from nio import (
        AsyncClient,
        AsyncClientConfig,
        LoginError,
        LoginResponse,
        MegolmEvent,
        RoomMessageText,
        SyncError,
        SyncResponse,
        UploadResponse,
        WhoamiError,
        WhoamiResponse,
    )
except ImportError:  # pragma: no cover
    print("[OFFICE] FATAL: falta matrix-nio. Instalar: pip install 'matrix-nio[e2e]'",
          flush=True)
    raise

TASK_MARKER = "NP_TASK:"
LOG_PREFIX = "[OFFICE]"
DEFAULT_DEVICE_ID = "NPOFFICEDESKTOP"
DEFAULT_DEVICE_NAME = "SIYAD Office Desktop"

Handler = Callable[["Task"], Any]


def log(event: str, **fields: Any) -> None:
    """Log a stdout/journal en formato clave=valor (grepeable, estilo NP)."""
    parts = [LOG_PREFIX, event]
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    print(" ".join(parts), flush=True)


@dataclass
class Task:
    task_id: str
    sender: str
    text: str
    locale: str = "es"
    raw: dict = field(default_factory=dict)


@dataclass
class TaskResult:
    text: str = ""
    files: list = field(default_factory=list)


class ConfigError(RuntimeError):
    pass


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class OfficeConfig:
    homeserver: str
    mxid: str
    token: str
    password: str
    device_id: str
    room_id: str
    room_alias: str
    bot_mxid: str
    task_url: str
    state_dir: str
    trusted: Tuple[str, ...]
    device_name: str = DEFAULT_DEVICE_NAME
    encrypted: bool = False
    sync_timeout: int = 30000
    backoff_start: float = 2.0
    backoff_max: float = 60.0
    env_file: str = ""

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "OfficeConfig":
        e = os.environ if env is None else env

        def _get(name: str, default: str = "") -> str:
            return (e.get(name) or default).strip()

        homeserver = _get("NP_MX_HOMESERVER").rstrip("/")
        mxid = _get("NP_MX_DESKTOP_MXID")
        token = _get("NP_MX_DESKTOP_TOKEN")
        password = _get("NP_MX_DESKTOP_PASSWORD")
        device_id = _get("NP_MX_DESKTOP_DEVICE_ID", DEFAULT_DEVICE_ID) or DEFAULT_DEVICE_ID
        room_id = _get("NP_MX_OFFICE_ROOM_ID")
        room_alias = _get("NP_MX_OFFICE_ALIAS").strip('"')
        bot_mxid = _get("NP_MX_BOT_MXID")
        task_url = _get("NP_OFFICE_TASK_URL")
        state_dir = _get("NP_OFFICE_STATE_DIR", "./.np_office") or "./.np_office"
        env_file = _get("NP_OFFICE_ENV_FILE")
        encrypted = _truthy(_get("NP_OFFICE_ENCRYPTED"))
        extra = [s.strip() for s in _get("NP_MX_TRUSTED_SENDERS").split(",") if s.strip()]
        trusted = tuple(dict.fromkeys([x for x in ([bot_mxid] + extra) if x]))

        missing = []
        if not homeserver:
            missing.append("NP_MX_HOMESERVER")
        if not mxid:
            missing.append("NP_MX_DESKTOP_MXID")
        if not (token or password):
            missing.append("NP_MX_DESKTOP_TOKEN o NP_MX_DESKTOP_PASSWORD")
        if not (room_id or room_alias):
            missing.append("NP_MX_OFFICE_ROOM_ID o NP_MX_OFFICE_ALIAS")
        if not trusted:
            missing.append("NP_MX_BOT_MXID")
        if missing:
            raise ConfigError("faltan variables de entorno: " + ", ".join(missing))

        try:
            sync_timeout = int(_get("NP_OFFICE_SYNC_TIMEOUT", "30000") or 30000)
        except ValueError:
            sync_timeout = 30000

        device_name = os.getenv("NP_MX_DEVICE_NAME") or DEFAULT_DEVICE_NAME
        return cls(
            homeserver=homeserver,
            mxid=mxid,
            token=token,
            password=password,
            device_id=device_id,
            device_name=device_name,
            room_id=room_id,
            room_alias=room_alias,
            bot_mxid=bot_mxid,
            task_url=task_url,
            state_dir=state_dir,
            trusted=trusted,
            encrypted=encrypted,
            sync_timeout=sync_timeout,
            env_file=env_file,
        )


class OfficeListener:
    """Listener Matrix de la app desktop: recibe tareas del bot y devuelve resultados."""

    # Refresco proactivo de device lists (NIO-KEYSYNC-01): cada N
    # segundos se piden los miembros de la sala y se fuerza un
    # keys_query con todos sus devices.
    DEVICE_REFRESH_SECS = 300

    def __init__(self, handler: Optional[Handler] = None,
                 config: Optional[OfficeConfig] = None) -> None:
        self.config = config or OfficeConfig.from_env()
        self.handler = handler
        self.client: Optional[AsyncClient] = None
        # La sala se resuelve por ALIAS en cada arranque: un room_id heredado
        # de un deploy anterior no debe usarse (ver _resolve_room).
        self._room_id: Optional[str] = None
        self._seen: set = set()
        self._joined: set = set()
        self._seen_order: list = []
        self._stop = asyncio.Event()
        self._device_refreshed_at: float = 0.0

    # ---------------------------------------------------------------- ejecutor
    async def _dispatch(self, task: Task) -> TaskResult:
        if self.handler is not None:
            result = self.handler(task)
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, TaskResult):
                return result
            if isinstance(result, str):
                return TaskResult(text=result)
            return TaskResult(text=str(result))

        if self.config.task_url:
            return await self._http_task(task)

        log("TASK_NO_EXECUTOR", task_id=task.task_id)
        return TaskResult(
            text=("No hay ejecutor configurado en esta app. Definir NP_OFFICE_TASK_URL "
                  "o instanciar OfficeListener(handler=...) con el handler de la app.")
        )

    async def _http_task(self, task: Task) -> TaskResult:
        import aiohttp  # dependencia de matrix-nio

        payload = {
            "task_id": task.task_id,
            "text": task.text,
            "locale": task.locale,
            "from": task.sender,
        }
        timeout = aiohttp.ClientTimeout(total=900)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.config.task_url, json=payload) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
        if status >= 400:
            log("TASK_HTTP_ERROR", status=status)
            return TaskResult(text=f"El ejecutor de la app devolvio HTTP {status}.")
        if isinstance(data, dict):
            answer = data.get("answer") or data.get("text") or ""
            files = data.get("files") or []
            return TaskResult(text=str(answer), files=[str(f) for f in files])
        return TaskResult(text=str(data))

    # ------------------------------------------------------------------ matrix
    # ------------------------------------------------------- credenciales vivas
    _ENV_KEYS: Dict[str, str] = {
        "NP_MX_DESKTOP_TOKEN": "token",
        "NP_MX_DESKTOP_PASSWORD": "password",
        "NP_MX_OFFICE_ROOM_ID": "room_id",
        "NP_MX_OFFICE_ALIAS": "room_alias",
    }

    def _reload_env_file(self, only_if_changed: bool = True) -> bool:
        """Relee el env que entrego el deploy. True si cambio algo relevante.

        Es el camino de reconexion multi-inquilino: cada empleado/cliente
        recibe SU env en SU deploy; si el proceso sigue vivo cuando esas
        credenciales caducan, se recargan en caliente y no hace falta
        reiniciar nada a mano.
        """
        cfg = self.config
        path = Path(cfg.env_file) if cfg.env_file else None
        if path is None or not path.is_file():
            return False
        try:
            raw = path.read_text()
        except OSError:
            return False

        fresh: Dict[str, str] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            fresh[key.strip()] = value.strip().strip('"').strip("'")

        changed = []
        for env_key, attr in self._ENV_KEYS.items():
            value = fresh.get(env_key, "")
            if value and value != getattr(cfg, attr):
                if only_if_changed:
                    setattr(cfg, attr, value)
                changed.append(env_key)
        if changed:
            log("CREDENTIALS_RELOADED", source=str(path), vars=",".join(changed))
            # Credencial nueva = deploy nuevo: la sala y el estado de union
            # tambien pueden haber cambiado.
            self._room_id = None
            self._joined.clear()
            self._device_refreshed_at = 0.0
        return bool(changed)

    async def _whoami(self) -> Tuple[bool, str]:
        """Valida el token contra el homeserver. (False, motivo) = caducado."""
        import aiohttp

        cfg = self.config
        if not cfg.token:
            return False, "sin_token"
        url = f"{cfg.homeserver}/_matrix/client/v3/account/whoami"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers={"Authorization": f"Bearer {cfg.token}"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    status = resp.status
                    data = await resp.json(content_type=None)
        except Exception as exc:
            # Red/HS caido: no hay informacion para declarar caducado.
            log("AUTH_PROBE_FAIL", exc_type=type(exc).__name__, exc=str(exc)[:160])
            return True, "probe_inaccesible"
        if status == 401 or (isinstance(data, dict) and data.get("errcode")):
            return False, str((data or {}).get("errcode") or f"http_{status}")
        return True, "ok"

    async def _server_device_keys(self, access_token: str) -> Optional[Dict[str, Any]]:
        """Claves de NUESTRO device en el homeserver. None = no se pudo ver."""
        import aiohttp

        cfg = self.config
        url = f"{cfg.homeserver}/_matrix/client/v3/keys/query"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={"device_keys": {cfg.mxid: []}},
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
        except Exception as exc:
            log("DEVICE_KEYS_PROBE_FAIL", exc_type=type(exc).__name__,
                exc=str(exc)[:160])
            return None
        if not isinstance(data, dict):
            return None
        devices = data.get("device_keys", {}).get(cfg.mxid, {}) or {}
        return dict(devices)

    def _reset_olm_store(self, reason: str) -> None:
        """Regenera la cuenta Olm local (el device del HS no tiene claves)."""
        store = Path(self.config.state_dir)
        if not store.is_dir():
            return
        removed = 0
        for path in store.glob("*"):
            if path.name == "device_id.txt":
                continue  # el device_id es del deploy y no cambia
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink()
                removed += 1
            except OSError:
                pass
        log("E2EE_STORE_RESET", reason=reason, removed=removed)

    async def _drop_client(self) -> None:
        """Suelta la sesion para que el proximo ciclo reloguee y revalide."""
        client, self.client = self.client, None
        self._room_id = None
        self._joined.clear()
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    async def _build_client(self) -> AsyncClient:
        cfg = self.config
        store = Path(cfg.state_dir)
        store.mkdir(parents=True, exist_ok=True)

        # device_id estable en disco: sin esto, cada arranque crea un device
        # nuevo mientras el store conserva la Olm del anterior -> E2EE mudo.
        dev_file = store / "device_id.txt"
        if dev_file.is_file():
            stored = dev_file.read_text().strip()
            if stored:
                cfg.device_id = stored
        else:
            dev_file.write_text(cfg.device_id)

        client = AsyncClient(
            cfg.homeserver,
            cfg.mxid,
            device_id=cfg.device_id,
            store_path=str(store),
            config=AsyncClientConfig(
                store_sync_tokens=True,
                encryption_enabled=True,
            ),
        )
        client.add_event_callback(self._on_message, RoomMessageText)
        client.add_event_callback(self._on_decryption_failure, MegolmEvent)

        if cfg.token:
            client.restore_login(cfg.mxid, cfg.device_id, cfg.token)
        else:
            # nio 0.25.x: login() NO acepta device_id (la firma es
            # password/device_name/token). El device_id FIJO va en el
            # constructor de AsyncClient (arriba) y nio lo reusa en Api.login.
            resp = await client.login(
                cfg.password, device_name=cfg.device_name,
            )
            if not isinstance(resp, LoginResponse):
                await client.close()
                raise RuntimeError(f"login fallo: {resp}")
            cfg.token = getattr(resp, "access_token", "") or cfg.token
            log("LOGIN_PASSWORD", mxid=cfg.mxid,
                device=getattr(resp, "device_id", "?"))
        return client

    async def _login(self) -> None:
        cfg = self.config

        # 0) Credenciales vivas: el env del deploy manda sobre el estado
        #    guardado en memoria de un ciclo anterior.
        if cfg.env_file:
            self._reload_env_file()

        # 1) El token se valida ANTES de confiar en el: un token de un deploy
        #    anterior (401) no debe dejar el listener "corriendo" en vano.
        if cfg.token:
            ok, detail = await self._whoami()
            if not ok:
                log("CREDENTIALS_STALE", detail=detail,
                    env_file=cfg.env_file or "-")
                if self._reload_env_file():
                    ok, detail = await self._whoami()
                if not ok:
                    raise RuntimeError(
                        f"CREDENTIALS_STALE ({detail}): sin token valido; "
                        "esperando credenciales nuevas del deploy"
                    )
            log("AUTH_OK", device_id=cfg.device_id, detail=detail)

        store = Path(cfg.state_dir)
        had_store = store.is_dir() and any(p.suffix == ".db" for p in store.glob("*"))

        # 2) Comprobacion REAL de si el homeserver tiene publicadas las claves
        #    de NUESTRO device, y ANTES de abrir el cliente: un store heredado
        #    de un deploy anterior hace que nio diga "already published" y
        #    nunca vuelva a subirlas -> nadie puede cifrarnos nada (la sala
        #    queda E2EE muda para siempre). Se consulta por HTTP plano para no
        #    dejar un cliente sqlite abierto por el camino (borrar el store con
        #    el cliente vivo tira "attempt to write a readonly database").
        if cfg.token and had_store:
            devices = await self._server_device_keys(cfg.token)
            # OJO: keys/query con la lista vacia devuelve TODOS los devices del
            # usuario, asi que hay que mirar SIEMPRE el nuestro.
            own_keys = (devices or {}).get(cfg.device_id)
            log("DEVICE_KEYS_PROBE", device_id=cfg.device_id,
                published=bool(own_keys))
            if devices is not None and not own_keys:
                self._reset_olm_store("hs_sin_claves_del_device")
                had_store = False
        elif had_store and not cfg.token:
            # login por password con store heredado: nio creeria que las claves
            # siguen publicadas y no subiria las del device recien creado.
            self._reset_olm_store("login_password_store_heredado")
            had_store = False

        client = await self._build_client()
        self.client = client
        log("LOGIN_TOKEN", mxid=cfg.mxid, device_id=cfg.device_id,
            store_reused=bool(had_store))
        await self._setup_e2ee()

    async def _setup_e2ee(self) -> None:
        """Gestión de claves Olm/Megolm (mismo orden que el bot NP)."""
        cfg = self.config
        client = self.client
        if client is None:
            return

        if getattr(client, "olm", None) is None:
            if cfg.encrypted:
                log("E2EE_OLM_MISSING",
                    device_id=cfg.device_id, store_path=cfg.state_dir,
                    hint="instalar matrix-nio[e2e] y verificar device_id.txt")
                raise RuntimeError(
                    "E2EE requerido (NP_OFFICE_ENCRYPTED=1) pero la máquina Olm "
                    "no cargó: instalar 'matrix-nio[e2e]' y revisar el store."
                )
            log("E2EE_DISABLED", reason="olm_ausente", encrypted=False)
            return

        log("E2EE_OLM_READY", device_id=cfg.device_id, store_path=cfg.state_dir)
        try:
            if client.should_upload_keys:
                await client.keys_upload()
                log("E2EE_KEYS_UPLOAD_OK", device_id=cfg.device_id)
            else:
                log("E2EE_KEYS_ALREADY_PUBLISHED", device_id=cfg.device_id)
            if client.should_query_keys:
                await client.keys_query()
                log("E2EE_KEYS_QUERY_OK", reason="startup")
        except Exception as exc:
            log("E2EE_KEY_MGMT_FAIL", exc_type=type(exc).__name__, exc=str(exc)[:200])

    async def _on_decryption_failure(self, room: Any, event: Any) -> None:
        log("DECRYPTION_FAIL", room_id=getattr(event, "room_id", "?"),
            event_id=getattr(event, "event_id", "?"),
            sender=getattr(event, "sender", "?"))

    async def _resolve_room(self, strict: bool = True) -> str:
        """Resuelve la sala de la oficina; el ALIAS es la fuente de verdad.

        El alias (#my-office-<app>-<usuario>) lo crea onboard.sh por usuario y
        es estable entre deploys, mientras que el room_id del env pertenece a
        UN deploy concreto: reutilizarlo tras un re-deploy mandaria las tareas
        a una sala que ya nadie escucha. Por eso:

          * con alias configurado -> se resuelve SIEMPRE el alias y si no
            resuelve se reintenta en el proximo ciclo (nunca room_id viejo);
          * sin alias -> se usa el room_id del env (implantaciones legacy).

        Si el alias deja de resolver en caliente (sala recreada por otro
        proceso), el cache se invalida y se vuelve a resolver.
        """
        cfg = self.config
        if self.client is None:
            raise RuntimeError("cliente no inicializado")

        if not cfg.room_alias:
            if not self._room_id and cfg.room_id:
                self._room_id = cfg.room_id
                log("ROOM_FROM_ENV", room_id=cfg.room_id)
            if not self._room_id:
                raise RuntimeError("no hay NP_MX_OFFICE_ALIAS ni NP_MX_OFFICE_ROOM_ID")
            return self._room_id

        resp = await self.client.room_resolve_alias(cfg.room_alias)
        room_id = getattr(resp, "room_id", None)
        if room_id:
            if room_id != self._room_id:
                self._room_id = room_id
                log("ROOM_RESOLVED", alias=cfg.room_alias, room_id=room_id)
            return room_id
        detail = str(getattr(resp, "message", resp))[:160]
        if self._room_id:
            # el alias dejo de resolver con la sala cacheada: se invalida, pero
            # si el caller no es estricto (enviar un resultado) se usa la sala
            # conocida en lugar de perder la respuesta del usuario.
            log("ROOM_ALIAS_RECHECK_FAIL", alias=cfg.room_alias, detail=detail)
            if not strict:
                return self._room_id
            self._room_id = None
            self._joined.clear()
        raise RuntimeError(f"no pude resolver la sala {cfg.room_alias}: {detail}")

    # ------------------------------------------------------------------ eventos
    def _accept(self, event: Any) -> bool:
        cfg = self.config
        if event.sender == cfg.mxid:
            return False
        if cfg.trusted and event.sender not in cfg.trusted:
            log("TASK_REJECTED_SENDER", sender=event.sender)
            return False
        if self._room_id and getattr(event, "room_id", None) != self._room_id:
            return False
        if not (event.body or "").lstrip().startswith(TASK_MARKER):
            return False
        if getattr(event, "event_id", "") in self._seen:
            return False
        return True

    async def _ensure_joined(self) -> None:
        """Une la cuenta desktop a la sala si aun no es miembro.

        Cuando onboard.sh crea la sala con el token del EMPLEADO, la cuenta
        @desktop-<usuario> queda solo INVITADA (npo_provision_instance invita
        a bot + desktop). Sin este join el listener queda "mudo": los eventos
        de la timeline no llegan a una cuenta solo invitada. join() de
        matrix-nio (NO room_join: ese metodo NO existe y tiraba
        AttributeError -> ROOM_JOIN_FAIL en bucle -> listener mudo) es
        idempotente en Synapse (si ya es miembro devuelve 200 con el room_id),
        asi que se llama una vez por room_id y se reintenta en cada ciclo de
        sync hasta lograrlo.
        """
        room_id = self._room_id or self.config.room_id
        if not room_id or room_id in self._joined:
            return
        try:
            resp = await self.client.join(room_id)
            joined = getattr(resp, "room_id", None)
            if joined:
                self._joined.add(joined)
                log("ROOM_JOINED", room=joined)
            else:
                log("ROOM_JOIN_FAIL", room=room_id,
                    detail=str(getattr(resp, "message", resp))[:200])
        except Exception as exc:
            # No es fatal: si el invite llega despues, el proximo ciclo de
            # sync/retry reintenta el join.
            log("ROOM_JOIN_FAIL", room=room_id, exc=str(exc)[:200])

    def _remember(self, event_id: str) -> None:
        if not event_id:
            return
        self._seen.add(event_id)
        self._seen_order.append(event_id)
        if len(self._seen_order) > 500:
            self._seen.discard(self._seen_order.pop(0))

    async def _on_message(self, room: Any, event: Any) -> None:
        try:
            if not self._accept(event):
                return
            self._remember(event.event_id)

            payload = (event.body or "").lstrip()[len(TASK_MARKER):].strip()
            try:
                data = json.loads(payload)
                if not isinstance(data, dict):
                    data = {"text": str(data)}
            except json.JSONDecodeError:
                data = {"text": payload}

            task = Task(
                task_id=str(data.get("task_id") or event.event_id),
                sender=str(data.get("from") or event.sender),
                text=str(data.get("text") or ""),
                locale=str(data.get("locale") or "es"),
                raw=data,
            )
            log("TASK_RECEIVED", task_id=task.task_id, chars=len(task.text))
            started = time.time()
            result = await self._dispatch(task)
            await self._post_result(result)
            log("TASK_DONE", task_id=task.task_id,
                seconds=round(time.time() - started, 2),
                files=len(result.files or []))
        except Exception as exc:  # nunca tumbar el listener por una tarea
            log("TASK_ERROR", exc_type=type(exc).__name__, exc=str(exc)[:300])

    async def _post_result(self, result: TaskResult) -> None:
        if self.client is None:
            raise RuntimeError("cliente no inicializado")
        # strict=False: si el alias falla en este instante se usa la sala ya
        # conocida; se prefiere entregar el resultado antes que perderlo.
        room_id = await self._resolve_room(strict=False)
        text = (result.text or "").strip() or "Tarea completada."

        # Device list fresca justo antes de compartir la session megolm
        # (NIO-KEYSYNC-01): cierra la carrera entre el sync que reporta un
        # device nuevo y la respuesta, para que ese device reciba las claves.
        if getattr(self.client, "should_query_keys", False):
            try:
                await self.client.keys_query()
                log("E2EE_KEYS_QUERY_OK", reason="pre_reply")
            except Exception as exc:
                log("KEY_QUERY_FAIL", exc_type=type(exc).__name__,
                    exc=str(exc)[:160])

        # Texto: nio cifra solo si la sala es E2EE (m.room.encrypted).
        await self.client.room_send(
            room_id,
            "m.room.message",
            {"msgtype": "m.text", "body": text},
            ignore_unverified_devices=True,
        )

        for candidate in (result.files or []):
            path = Path(str(candidate))
            if not path.is_file():
                log("FILE_MISSING", path=str(path))
                continue
            data = path.read_bytes()
            # En sala cifrada el content NO lleva "url": lleva "file" con las
            # claves de descifrado (patrón idéntico a _send_media del bot).
            room_obj = self.client.rooms.get(room_id)
            encrypt = bool(getattr(room_obj, "encrypted", False))
            try:
                resp, keys = await self.client.upload(
                    io.BytesIO(data),
                    content_type="application/octet-stream",
                    filename=path.name,
                    encrypt=encrypt,
                    filesize=len(data),
                )
            except Exception as exc:
                log("UPLOAD_FAIL", name=path.name, exc_type=type(exc).__name__,
                    exc=str(exc)[:200])
                continue
            if not isinstance(resp, UploadResponse):
                log("UPLOAD_FAIL", name=path.name, detail=str(resp)[:200])
                continue

            content = {
                "msgtype": "m.file",
                "body": path.name,
                "info": {"size": len(data), "mimetype": "application/octet-stream"},
            }
            if encrypt and keys:
                keys["url"] = resp.content_uri
                content["file"] = keys
            else:
                content["url"] = resp.content_uri

            await self.client.room_send(
                room_id, "m.room.message", content,
                ignore_unverified_devices=True,
            )
            log("FILE_SENT", name=path.name, bytes=len(data), encrypted=encrypt)

    async def _key_maintenance(self) -> None:
        """Tareas de claves que AsyncClient.sync_forever() ejecuta y
        AsyncClient.sync() NO (NIO-KEYSYNC-01).

        sync() baja el sync y nada más: no consulta device lists nuevas,
        no sube One-Time Keys ni manda los to-device encolados. Sin esto:

        * Un device creado DESPUÉS del arranque (el QR de Element X del
          empleado, un device nuevo del bot) sigue siendo desconocido y
          share_group_session() no le reparte la session megolm: ese chat
          se queda en "unable to decrypt" para siempre.
        * Las OTKs no se renuevan; agotadas, nadie puede abrir sesión
          Olm con nosotros y dejan de llegarnos tareas.
        * Las key requests se quedan encoladas y no se piden las
          sessions que faltan.
        """
        if self.client is None or self.client.olm is None:
            return
        try:
            # Refresco proactivo de las device lists de la sala
            # (NIO-KEYSYNC-01): `device_lists.changed` sólo notifica cambios
            # posteriores al sync, así que un device creado mientras el
            # listener estaba caído quedaría desconocido para siempre y no
            # recibiría las claves de nuestras respuestas. Se fuerza el
            # keys_query completo (nio devuelve todos los devices de esos
            # usuarios) al arrancar y cada DEVICE_REFRESH_SECS.
            now = time.time()
            if now - self._device_refreshed_at >= self.DEVICE_REFRESH_SECS:
                room_id = self._room_id or self.config.room_id
                if room_id:
                    # nio NO persiste los miembros de la sala y, con
                    # store_sync_tokens=True, los sync con `since` no vuelven
                    # a traer el state: room.users llega vacío y sin esa
                    # lista no hay manera de saber a qué devices consultar.
                    # joined_members() devuelve los miembros de la sala y
                    # con ellos se fuerza un keys_query, que pide TODOS los
                    # devices de esos usuarios. OJO: nio ignora esa
                    # respuesta si la sala aún no está en client.rooms
                    # (al arrancar sólo aparece cuando llega un evento), así
                    # que la lista se lee de la respuesta, no de room.users.
                    resp = await self.client.joined_members(room_id)
                    users = {m.user_id
                             for m in getattr(resp, "members", None) or []}
                    if users:
                        self.client.olm.add_changed_users(users)
                    log("E2EE_DEVICE_REFRESH", room=room_id[:16],
                        users=len(users))
                    self._device_refreshed_at = now
            if self.client.should_query_keys:
                await self.client.keys_query()
                log("E2EE_KEYS_QUERY_OK", reason="maintenance")
            if self.client.should_upload_keys:
                await self.client.keys_upload()
            if self.client.should_claim_keys:
                await self.client.keys_claim(
                    self.client.get_users_for_key_claiming())
            await self.client.send_to_device_messages()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # La ventana NO se consume: si algo falla (red, HS caída) se
            # reintenta en el siguiente ciclo en lugar de esperar N segundos.
            self._device_refreshed_at = 0.0
            log("KEY_MAINT_FAIL", exc_type=type(exc).__name__,
                exc=str(exc)[:200])

    # --------------------------------------------------------------- ciclo vida
    async def run_forever(self) -> None:
        cfg = self.config
        executor = "handler" if self.handler else ("http" if cfg.task_url else "none")
        log("BOOT", mxid=cfg.mxid, homeserver=cfg.homeserver,
            room=(cfg.room_alias or cfg.room_id), executor=executor,
            device_id=cfg.device_id, device_name=cfg.device_name, e2ee=cfg.encrypted,
            trusted=",".join(cfg.trusted), env_file=cfg.env_file or "-")

        backoff = cfg.backoff_start
        last_env_check = 0.0
        while not self._stop.is_set():
            try:
                # El env del deploy manda mientras el proceso viva: si el
                # deploy escribe credenciales nuevas (rotacion diaria, otro
                # empleado/cliente), se cambia de sesion sin reiniciar nada.
                if cfg.env_file and time.time() - last_env_check >= 60:
                    last_env_check = time.time()
                    if self._reload_env_file() and self.client is not None:
                        log("SESSION_RESET", reason="credenciales_nuevas_del_deploy")
                        await self._safe_close()
                        continue

                if self.client is None:
                    await self._login()
                await self._resolve_room()
                await self._ensure_joined()
                resp = await self.client.sync(timeout=cfg.sync_timeout)
                if isinstance(resp, SyncError):
                    status = getattr(resp, "status_code", None)
                    detail = str(getattr(resp, "message", resp))[:200]
                    if status in (401, 403) or "M_UNKNOWN_TOKEN" in detail:
                        # token caducado/revocado: se cierra y en el proximo
                        # ciclo _login() revalida y relee el env del deploy.
                        log("CREDENTIALS_STALE", detail=f"http_{status}",
                            env_file=cfg.env_file or "-")
                    raise RuntimeError(f"sync fallo ({status}): {detail}")
                await self._key_maintenance()
                backoff = cfg.backoff_start
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log("SYNC_RETRY", exc_type=type(exc).__name__,
                    exc=str(exc)[:200], sleep=round(backoff, 1))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, cfg.backoff_max)
                await self._safe_close()

        await self._safe_close()
        log("STOPPED")

    async def _safe_close(self) -> None:
        if self.client is None:
            return
        try:
            await self.client.close()
        except Exception:
            pass
        finally:
            self.client = None

    def stop(self) -> None:
        self._stop.set()


def selfcheck(config: OfficeConfig) -> int:
    print(f"{LOG_PREFIX} SELFCHECK")
    print(f"  homeserver : {config.homeserver}")
    print(f"  mxid       : {config.mxid}")
    print(f"  auth       : {'token' if config.token else 'password'}")
    print(f"  device_id  : {config.device_id}")
    print(f"  room       : {config.room_alias or config.room_id}")
    if config.room_alias and config.room_id:
        print(f"  room_id    : {config.room_id} (solo fallback; el alias manda)")
    print(f"  e2ee       : {'requerido' if config.encrypted else 'opcional/no exigido'}")
    print(f"  confiables : {', '.join(config.trusted)}")
    print(f"  ejecutor   : {config.task_url or 'handler Python (solo via API)'}")
    print(f"  device_name: {config.device_name}")
    print(f"  state_dir  : {config.state_dir}")
    print(f"  env_file   : {config.env_file or '(no relee credenciales en caliente)'}")
    print(f"  sync       : {config.sync_timeout} ms")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="SIYAD Pocket-to-Office listener")
    parser.add_argument("--selfcheck", action="store_true",
                        help="valida la configuracion y sale (no conecta)")
    args = parser.parse_args(argv)

    try:
        config = OfficeConfig.from_env()
    except ConfigError as exc:
        log("CONFIG_ERROR", detail=str(exc))
        return 2

    if args.selfcheck:
        return selfcheck(config)

    listener = OfficeListener(config=config)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, listener.stop)
        except (NotImplementedError, AttributeError):
            pass  # Windows

    try:
        loop.run_until_complete(listener.run_forever())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
