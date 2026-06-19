# AGENTS.md

Streamlit UI over a stdlib-only WhatsApp-chat parser for diamond/RC drilling
daily reports. Two top-level files, no packages: `script.py` (parser + aggregator,
imported as a module) and `app.py` (Streamlit UI).

## Commands

```bash
./run.sh [port]        # preferred: clears __pycache__, activates .venv if present,
                       #   runs streamlit headless on 8501 (or custom port)
python3 script.py --tests   # validation only, writes nothing. exit 0 = clean, 2 = issues
python3 script.py           # writes avance_semanal.csv + reporte.md from _chat.txt
```

- `run.sh` deletes `__pycache__` on every launch to avoid stale `ImportError`s
  after large edits (see `run.sh` header). Prefer it over `streamlit run app.py`.
- No linter/formatter/typecheck/CI is configured. `.gitignore` lists
  `.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/` defensively but none run.
  Verify changes with `python3 script.py --tests` (clean = exit 0).

## Architecture

- **`app.py` imports `script` as a module** (`from script import ...`, app.py:28).
  Keep `script.py` stdlib-only and free of import-time side effects so the UI can
  load it. The UI is also added to `script.py`'s surface — don't add UI logic to
  the parser file; add it to `app.py`.
- **`st.set_page_config` runs at module level** in `app.py:48` and must stay the
  first Streamlit call. The UI body is wrapped in `main()` (app.py:585) so
  importing the module for tests does not trigger widgets. `st.stop()` is
  guarded with a following `return` (app.py:631) because it doesn't stop outside
  a session context — keep that pattern.
- **`width="stretch"` is used everywhere** (not the deprecated
  `use_container_width`). Streamlit 2.0 API — don't regress it.

## Parser (script.py)

- **Dual-campaign, not diamond-only.** `normalize_well_id` handles `ZD-NNNN`
  (diamond) and `PRI-NN` / `PP-NN` (RC); `normalize_phase` returns a canonical
  `FASE_N` key but also keeps non-standard sectors (e.g. `PINTA_VERDE_3`) as
  pseudo-columns. README and `reporte.md` only document the diamond chat — do
  not assume the code is diamond-only.
- **Two WhatsApp export formats are supported:** iOS
  `[DD-MM-YY, HH:MM:SS a. m.]` and Android `D/M/YYYY, HH:MM -`. A U+200E
  left-to-right mark may prefix headers (match it optionally, see
  `IOS_HEADER_RE`/`ANDROID_HEADER_RE`, script.py:322).
- **Field regex landmines** (script.py:415): the field-name part uses
  **non-greedy `[^\n:]*?`** — a greedy `*` silently truncates values
  (`Pozo. PRI-02` → `02`). The `fondo`/`avance`/`programado` patterns also
  absorb drill-bit fractions before the value, e.g.
  `Fondo 13 3/4: 90.00 mts`; if you touch these, re-run `--tests`.
- **Dates:** week aggregation uses the **message header date** (`msg_dt.date()`),
  not the body date — body dates carry typos (resends with stale dates).
- **Reubicaciones:** a well toggling phases between shifts (e.g. ZD-2833 in
  FASE 12 then FASE 13) is assigned to the **dominant phase** for that week
  (`dominant_phase`, script.py:544). Don't "fix" this as a bug.
- **Validation tolerance is ±0.1 m** (`TOLERANCE_M`, script.py:76).

## Conventions

- **Language split:** UI strings, user-facing labels, and technical notes are in
  **neutral Spanish (no voseo)** — see commit `cda0fe3`. Code, identifiers, and
  comments are in English. Match the user's Spanish in chat replies.
- **`avance_semanal.csv` and `reporte.md` are tracked in the repo** as example
  outputs (the gitignore lines for them are commented out). They are
  regenerable; don't treat them as gitignored or hand-edit them.
- **`_chat.txt` is the example diamond chat** (committed). The RC chat is **not**
  in the repo — it lives outside (e.g. `~/Descargas/`) and is loaded via the UI
  uploader. The app never reads chat files from disk; upload is always manual.
