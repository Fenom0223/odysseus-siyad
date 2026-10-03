"""Lectura DINÁMICA de las credenciales de LiteLLM — OFFICE-LLM-401-02.

Las 3 apps desktop (Eigent, RAG, Odysseus) leen NP_OFFICE_LLM_KEY /
OPENAI_COMPATIBLE_API_KEY_LLM del .env SOLO al arrancar el proceso. Cuando
LiteLLM rota su master key en un re/deploy, esas keys quedan viejas y toda
tarea de oficina devuelve 401 silencioso en la sala (el listener lo registra
como TASK_DONE porque el executor responde 200 con el error dentro del texto).

Este módulo relee el runtime compartido en CADA llamada, con coste de un
`os.stat` (guardado por mtime): una key nueva publicada por
refresh_llm_creds.py se recoge SIN reiniciar el proceso.

Sólo se toca la KEY (el secreto que rota); la URL nunca se pisa, porque cada
app tiene la suya (NP_OFFICE_LLM_URL / OPENAI_COMPATIBLE_BASE_URL_LLM).

Uso:
    from np_llm_creds import ensure
    ensure()          # al principio de la función que llama al LLM
"""
import os

CREDS = "/persistent/config-dirs/np-office/litellm.creds"
KEY_VARS = (
    "NP_OFFICE_LLM_KEY",           # Eigent / Odysseus
    "OPENAI_COMPATIBLE_API_KEY_LLM",  # RAG (esperanto/open_notebook)
    "OPENAI_API_KEY",              # SDK universal de OpenAI
)


def ensure(force: bool = False) -> bool:
    """Aplica la key vigente a os.environ. Devuelve True si cambió algo."""
    try:
        st = os.stat(CREDS)
    except OSError:
        # Sin runtime: dejamos lo que haya en el entorno (arranque clásico).
        return False
    if not force and getattr(ensure, "_mtime", None) == st.st_mtime:
        return False

    try:
        with open(CREDS, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() == "LITELLM_KEY" and value.strip():
                    for var in KEY_VARS:
                        os.environ[var] = value.strip()
                    ensure._mtime = st.st_mtime
                    return True
    except OSError:
        return False
    return False
