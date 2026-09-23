# DOE — Directivas operativas (Directive · Orchestration · Execution)

Estándar DOE (arquitectura de 3 capas de Nick Saraev): las **directivas** son
SOPs en Markdown, el **orquestador** (LLM de la app) rutea y ejecuta, y la
**capa de ejecución** son scripts deterministas. Fuente: `/persistent/doe/
AGENTIC WORKFLOWS DOCS - NICK SARAEV/GEMINI.md`.

> Alcance: **solo directivas**. Los assets `.csv` de RAG (`docs/rag/`) NO se
> empaquetan: se adjuntan manualmente en las demos.

## Formato estándar de una directiva

`doe/directives/<slug>.md`:

```markdown
---
name: doe-<slug>              # kebab-case, único
description: <una línea>      # lo que ve el orquestador para rutar
version: 1.0.0
category: doe
tags: [doe, siyad-demo, ...]
status: published             # draft NO se indexa en Odysseus
confidence: 0.95
source: imported
created: <ISO8601>
---
# DOE-<ID> — <slug>.md
OPERATIONAL EXECUTION DIRECTIVE (DOE) · <PROYECTO>

## TRIGGER      # qué dice/qué evento lo dispara
## INPUTS       # datos del caso
## STEPS        # pasos deterministas (mapea a "procedure" en Odysseus)
## CONFIRMATION # salida esperada en pantalla
## LOG          # registro del resultado
```

Directivas incluidas: `approval_delegation` (DOE-3A), `churn_radar` (DOE-2A),
`customs_inspection_amendment` (DOE-11A), `demurrage_detention` (DOE-9A),
`pricing_navieras` (DOE-7A), `tracking_consolidado` (DOE-5A).

## Dónde se cargan (sembrado)

Los ficheros canónicos viajan en el repo (`doe/directives/`). Cada app los
carga desde su raíz de skills, así que hay que sembrarlos (una vez, o en el
entrypoint del futuro docker):

| App      | Raíz de runtime (las escanea automáticamente)              |
|----------|------------------------------------------------------------|
| Odysseus | `data/skills/doe/<slug>/SKILL.md` (`data/` es gitignored)  |
| Eigent   | `~/.eigent/skills/<slug>/SKILL.md` (API + frontend + CAMEL)|

```bash
# sembrar en ambos (desde la raíz de cada repo)
for f in doe/directives/*.md; do
  slug=$(basename "$f" .md)
  mkdir -p "data/skills/doe/$slug"      # Odysseus
  cp "$f" "data/skills/doe/$slug/SKILL.md"
  mkdir -p "$HOME/.eigent/skills/$slug" # Eigent
  cp "$f" "$HOME/.eigent/skills/$slug/SKILL.md"
done
```

## Mapeo de las 3 capas en cada app

| Capa          | Odysseus                                              | Eigent                                                     |
|---------------|-------------------------------------------------------|------------------------------------------------------------|
| Directive     | skill en `data/skills/doe/` → `manage_skills view`     | skill en `~/.eigent/skills/` → `GET /skills` (brain :5001)  |
| Orchestration | inyección en `agent_loop` + `/api/tasks` + `/office/task` | `SkillToolkit` (CAMEL) + triggers `/api/v1/trigger/` + `/office/task` |
| Execution     | `BUILTIN_ACTIONS` (`run_local`/`run_script`) sin LLM   | `code_execution_toolkit` / `terminal_toolkit` / futuro `doe_toolkit` |
