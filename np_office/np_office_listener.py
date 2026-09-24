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
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

try:
    from nio import (
        AsyncClient,
        AsyncClientConfig,
        LoginResponse,
        MegolmEvent,
        RoomMessageText,
        UploadResponse,
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
        )


class OfficeListener:
    """Listener Matrix de la app desktop: recibe tareas del bot y devuelve resultados."""

    def __init__(self, handler: Optional[Handler] = None,
                 config: Optional[OfficeConfig] = None) -> None:
        self.config = config or OfficeConfig.from_env()
        self.handler = handler
        self.client: Optional[AsyncClient] = None
        self._room_id: Optional[str] = self.config.room_id or None
        self._seen: set = set()
        self._joined: set = set()
        self._seen_order: list = []
        self._stop = asyncio.Event()

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
    async def _login(self) -> None:
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
            log("LOGIN_TOKEN", mxid=cfg.mxid, device_id=cfg.device_id)
        else:
            # nio 0.25.x: login() NO acepta device_id (la firma es
            # password/device_name/token). El device_id FIJO va en el
            # constructor de AsyncClient (arriba) y nio lo reusa en Api.login.
            resp = await client.login(
                cfg.password, device_name=cfg.device_name,
            )
            if not isinstance(resp, LoginResponse):
                raise RuntimeError(f"login fallo: {resp}")
            log("LOGIN_PASSWORD", mxid=cfg.mxid,
                device=getattr(resp, "device_id", "?"))

        self.client = client
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

    async def _resolve_room(self) -> str:
        if self._room_id:
            return self._room_id
        if self.client is None:
            raise RuntimeError("cliente no inicializado")
        resp = await self.client.room_resolve_alias(self.config.room_alias)
        room_id = getattr(resp, "room_id", None)
        if not room_id:
            raise RuntimeError(f"no pude resolver la sala {self.config.room_alias}: {resp}")
        self._room_id = room_id
        log("ROOM_RESOLVED", alias=self.config.room_alias, room_id=room_id)
        return room_id

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
        de la timeline no llegan a una cuenta solo invitada. room_join es
        idempotente en Synapse (si ya es miembro devuelve 200 con el room_id),
        asi que se llama una vez por room_id y se reintenta en cada ciclo de
        sync hasta lograrlo.
        """
        room_id = self.config.room_id
        if not room_id or room_id in self._joined:
            return
        try:
            resp = await self.client.room_join(room_id)
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
        room_id = await self._resolve_room()
        text = (result.text or "").strip() or "Tarea completada."

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

    # --------------------------------------------------------------- ciclo vida
    async def run_forever(self) -> None:
        cfg = self.config
        executor = "handler" if self.handler else ("http" if cfg.task_url else "none")
        log("BOOT", mxid=cfg.mxid, homeserver=cfg.homeserver,
            room=(cfg.room_id or cfg.room_alias), executor=executor,
            device_id=cfg.device_id, device_name=cfg.device_name, e2ee=cfg.encrypted,
            trusted=",".join(cfg.trusted))

        backoff = cfg.backoff_start
        while not self._stop.is_set():
            try:
                if self.client is None:
                    await self._login()
                await self._resolve_room()
                await self._ensure_joined()
                await self.client.sync(timeout=cfg.sync_timeout)
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
    print(f"  room       : {config.room_id or config.room_alias}")
    print(f"  e2ee       : {'requerido' if config.encrypted else 'opcional/no exigido'}")
    print(f"  confiables : {', '.join(config.trusted)}")
    print(f"  ejecutor   : {config.task_url or 'handler Python (solo via API)'}")
    print(f"  device_name: {config.device_name}")
    print(f"  state_dir  : {config.state_dir}")
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
