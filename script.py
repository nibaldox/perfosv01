#!/usr/bin/env python3
"""
Diamond drilling weekly progress analyzer.

Reads a WhatsApp chat export with daily drilling reports and produces:
  * avance_semanal.csv - weekly progress table by phase
  * reporte.md         - markdown report with the same table and technical notes

The parser handles two common chat styles plus minor variants:
  * Uppercase / "Robinson" style  (POZO, SECTOR, INICIO., FONDO, AVANCE, CASING HWT)
  * Title case / "Eduardo" style  (Pozo, Sector, Desde, Hasta, Perforado, Casing HWT)

Reubicaciones (a well that toggles phase labels between shifts, e.g. ZD-2833) are
resolved by assigning the well to the dominant phase in that week and computing
net advance from the global min(inicio) / max(fondo) of all its reports.

Usage:
    python script.py                                  # default _chat.txt -> outputs
    python script.py --input chat.txt --csv out.csv --md out.md
    python script.py --tests                         # validate only, no output
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Column order priority. Phases present in the data are emitted in this
# order first; any phase discovered dynamically (e.g. FASE 15 from a campaign
# we haven't seen) is appended in sorted order.
#
# The leading order ("FASE_14, FASE_9, FASE_8S, FASE_12, FASE_13") is the
# canonical order required by the diamond-drilling spec; the remaining
# entries give us a sensible default for new campaigns (RC, geotech, etc.)
# without losing the diamond-chat table layout.
PHASE_KEYS: list[str] = [
    "FASE_14", "FASE_9", "FASE_8S", "FASE_12", "FASE_13", "FASE_8", "FASE_15",
]
PHASE_LABELS: dict[str, str] = {
    "FASE_8": "FASE 8",
    "FASE_8S": "FASE 8S",
    "FASE_9": "FASE 9",
    "FASE_12": "FASE 12",
    "FASE_13": "FASE 13",
    "FASE_14": "FASE 14",
    "FASE_15": "FASE 15",
}


def present_phases(reports: list[Report]) -> list[str]:
    """Return the phase keys actually present in the data, in the order
    defined by PHASE_KEYS with any unknown keys appended (sorted)."""
    found = {r.phase for r in reports}
    ordered = [k for k in PHASE_KEYS if k in found]
    extras = sorted(found - set(PHASE_KEYS))
    return ordered + extras

MONTH_LABELS = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

# Tolerance (in metres) when comparing computed vs reported values.
TOLERANCE_M = 0.1

# Keywords in observations that mark a well as finished in that shift.
FINISH_KEYWORDS = (
    "finaliz",            # finalizado / finaliza
    "pozo finalizado",
    "desarme",
    "se inicia desarme",
    "traslad",            # traslado / traslada
    "se entrega en traslado",
    "se entrega en trasl",
    "retira totalidad",
    "sale con dificultad",
    "movimiento de equipos",
    "pozo en traslado",
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Report:
    """A single drilling shift report parsed out of the chat."""

    report_date: date
    shift: str                # "Día" or "Noche"
    equipo: str
    pozo: str
    sector_raw: str
    phase: str                # normalized key, e.g. "FASE_14"
    inclination: Optional[str]
    programado: Optional[float]
    inicio: Optional[float]
    fondo: Optional[float]
    avance: Optional[float]
    casing_hwt: Optional[float]
    observaciones: str


@dataclass
class WeeklyRow:
    """A row in the weekly progress table."""

    week_start: date
    month_label: str
    phases: dict               # phase_key -> metres
    active_wells: list         # well labels with * / ** markers
    total: float


@dataclass
class PeriodRow:
    """A row in the strict per-period (daily / monthly) aggregation.

    Unlike :class:`WeeklyRow`, the metres here are the *literal* sum of
    ``Perforado`` over the reports that fall in the period. There is no
    net-advance calculation, so a well that crosses a month boundary has
    its metres attributed to the period in which each report was issued —
    not stretched across the boundary. Summing all ``PeriodRow.total``
    values equals the raw ``Σ Perforado`` of the dataset.
    """

    period_start: date          # first day of the period (always day=1 for monthly)
    period_label: str           # e.g. "Marzo 2026" or "11/03/26"
    phases: dict                # phase_key -> metres
    active_wells: list          # well labels (deduplicated, no markers)
    total: float


@dataclass
class WellSummary:
    """Per-well aggregate built from a list of reports. Used by the
    'Resumen por Pozo' table and the per-well charts."""

    pozo: str
    phases: list                  # sorted unique phase keys
    total_m: float                # sum of Perforado for this well
    inicio: float                 # min of all inicio values (0 if unknown)
    fondo: float                  # max of all fondo values  (0 if unknown)
    avg_m_per_shift: float        # total_m / num_shifts
    casing_total: float           # deepest casing installed (max of casing_hwt)
    num_shifts: int               # total reports for this well
    num_active_shifts: int        # reports with avance > 0
    num_weeks: int                # unique weeks with any report
    first_date: Optional[date]
    last_date: Optional[date]
    has_started: bool             # well has a 0/0/0 'inicio' record
    has_finished: bool            # well has a 'finalizado/desarme/traslado' record


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------

def parse_number(raw: object) -> Optional[float]:
    """Parse a number that may use comma, dot, or a single space as decimal
    separator, and may carry trailing units such as 'm', 'mts', 'mt', '%', '°'.

    Chilean mining reports sometimes use a space as a decimal mark
    (e.g. "4 50 mts" meaning 4.50 m, or "16 45 m" meaning 16.45 m). We treat
    space-separated digits as "<int>.<frac>" — but only when every part is
    purely numeric, so a sign like "- 60" is not misread as "-.60".

    Examples:
        parse_number("115,00 mts") -> 115.0
        parse_number("28.5m")      -> 28.5
        parse_number("4 50 mt")    -> 4.5
        parse_number("16 45 m")    -> 16.45
        parse_number("- 60°")      -> -60.0
        parse_number("")           -> None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Strip trailing non-numeric junk (units, degree signs, percent).
    s = re.sub(r"[^\d\s,.\-]+$", "", s).strip()
    if not s:
        return None
    # Decimal-with-space: "4 50" -> 4.50, "16 45" -> 16.45. Only when every
    # space-separated token is purely numeric.
    parts = s.split()
    if len(parts) >= 2 and all(re.fullmatch(r"\d[\d,.]*", p) for p in parts):
        s = parts[0] + "." + "".join(parts[1:])
    else:
        s = s.replace(" ", "")
    # Comma/dot handling: when both are present, the rightmost is the decimal.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    s = re.sub(r"[^0-9.\-]", "", s)
    # Strip trailing dots — a common chat typo ("28.55." should read 28.55).
    s = s.rstrip(".")
    if not s or s in ("-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Phase normalization
# ---------------------------------------------------------------------------

def normalize_well_id(raw: str) -> str:
    """Normalize a well identifier to merge cosmetic variations of the same
    physical well.

    "PRI 02", "PRI-02", "Pri-2", "PRI-2"          -> "PRI-02"
    "PRI 5",  "PRI-05", "Pri-5"                   -> "PRI-05"
    "PRI-1B", "PRI-1-B", "PRI1B", "PRI1-B"        -> "PRI-01-B"
    "PP-5",   "PP-05"                             -> "PP-05"
    "ZD-2817"                                     -> "ZD-2817"
    "PRI-4 / traslado a PP-05"                    -> "PRI-04"  (annotation dropped)
    """
    if not raw:
        return raw
    s = raw.strip().upper()
    if "/" in s:
        s = s.split("/", 1)[0].strip()
    m = re.match(r"^([A-Z]+)[\s\-_]*0*(\d+)(.*)$", s)
    if m:
        prefix, num, suffix = m.groups()
        suffix = re.sub(r"[\s\-_]+", "", suffix)
        return f"{prefix}-{int(num):02d}" + (f"-{suffix}" if suffix else "")
    # Fallback: at least strip cosmetic chars
    return re.sub(r"[\s\-_]+", "", s)


def normalize_phase(raw: str) -> Optional[str]:
    """Normalize a sector/phase name to a canonical key.

    Standard cases produce ``FASE_N`` (or ``FASE_8S`` for the 8 sur variant).
    Non-standard sector names that don't start with "FASE" (e.g. "Pinta
    verde 3" from a hydrogeological campaign) are returned as cleaned
    pseudo-keys so they still appear as their own column in the weekly table.

    Examples:
        "Fase 14"               -> "FASE_14"
        "FASE 8 sur"            -> "FASE_8S"
        "FASE 8S"               -> "FASE_8S"
        "FASE 8 S"              -> "FASE_8S"
        "FASE 09"               -> "FASE_9"
        "Fase 15 restringidos"  -> "FASE_15"   (trailing annotation dropped)
        "Fase 9W"               -> "FASE_9"    (suffix letter dropped)
        "Fase 9 oeste"          -> "FASE_9"
        "Pinta verde 3"         -> "PINTA_VERDE_3"
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    s_upper = s.upper()

    # 8 sur / 8S / 8 S / 8 SUR -> 8S
    if re.match(r"^FASE\s+8\s*S(UR)?\b", s_upper):
        return "FASE_8S"

    # Standard "Fase N" / "FASE N" pattern
    m = re.match(r"^FASE\s+(\d+)", s_upper)
    if m:
        return f"FASE_{int(m.group(1))}"

    # Non-standard sector name: keep it (cleaned up) as a pseudo-key so
    # the data still shows up in the report.
    cleaned = re.sub(r"\s+", "_", s_upper.strip())
    if cleaned and cleaned != "FASE":
        return cleaned
    return None


def phase_label(phase_key: str) -> str:
    """Display label for a phase key. Falls back to the raw key for
    non-standard sectors, and prettifies ``FASE_N`` keys not in the
    PHASE_LABELS table (so a future FASE_16 would still render as
    "FASE 16" without crashing)."""
    if phase_key in PHASE_LABELS:
        return PHASE_LABELS[phase_key]
    if phase_key.startswith("FASE_"):
        return f"FASE {phase_key[5:]}"
    # Custom sector (e.g. "PINTA_VERDE_3" -> "Pinta verde 3").
    return phase_key.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Chat splitting
# ---------------------------------------------------------------------------

# WhatsApp exports come in two flavours. We support both:
#
# * iOS:   [DD-MM-YY, HH:MM:SS a. m.]  Sender: body…
# * Android: D/M/YYYY, HH:MM -  Sender: body…
#
# Some headers are prefixed with a U+200E LEFT-TO-RIGHT MARK that WhatsApp
# injects around "imagen omitida" / "documento omitido" / "Se eliminó" lines.

IOS_HEADER_RE = re.compile(
    r"^\u200e?\[(\d{1,2})[-/](\d{1,2})[-/](\d{2,4}),\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})\s*([ap]\.?\s*m\.?)\]\s*(.*)$"
)

ANDROID_HEADER_RE = re.compile(
    r"^\u200e?(\d{1,2})/(\d{1,2})/(\d{2,4}),\s*"
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*-\s*(.*)$"
)

# Date embedded in the report body, e.g. "información 16.04.2026".
BODY_DATE_RE = re.compile(
    r"informaci[oó]n\s+(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})",
    re.IGNORECASE,
)

# Shift marker: "Turno Día" / "Turno Noche" / "TURNO NOCHE".
SHIFT_RE = re.compile(r"Turno\s+(D[ií]a|Noche)\b", re.IGNORECASE)


def _parse_header(line: str) -> Optional[tuple[datetime, str]]:
    """Try to parse a message header (iOS or Android format).

    Returns ``(datetime, post_header_text)`` on success, ``None`` if the line
    is not a message boundary."""
    m = IOS_HEADER_RE.match(line)
    if m:
        d, mo, y, h, mi, s, _ampm, tail = m.groups()
        year = int(y) + 2000 if len(y) == 2 else int(y)
        try:
            dt = datetime(year, int(mo), int(d), int(h), int(mi), int(s))
        except ValueError:
            return None
        return dt, tail.strip()
    m = ANDROID_HEADER_RE.match(line)
    if m:
        d, mo, y, h, mi, s, tail = m.groups()
        year = int(y) + 2000 if len(y) == 2 else int(y)
        si = int(s) if s is not None else 0
        try:
            dt = datetime(year, int(mo), int(d), int(h), int(mi), si)
        except ValueError:
            return None
        return dt, tail.strip()
    return None


def split_messages(text: str) -> list[tuple[Optional[datetime], str]]:
    """Split a chat export into (timestamp, body) pairs.

    Supports both the iOS ``[DD-MM-YY, HH:MM:SS a. m.]`` format and the
    Android ``D/M/YYYY, HH:MM -`` format. The first message in the file is
    usually the system "messages are end-to-end encrypted" notice and may
    have a None timestamp; we keep it so callers can skip it.
    """
    lines = text.splitlines()
    messages: list[tuple[Optional[datetime], str]] = []
    current_dt: Optional[datetime] = None
    current_body: list[str] = []

    for line in lines:
        header = _parse_header(line)
        if header is not None:
            if current_dt is not None or current_body:
                messages.append(
                    (current_dt, "\n".join(current_body).strip("\n"))
                )
            current_dt, tail = header
            current_body = [tail] if tail else []
        else:
            current_body.append(line)
    if current_dt is not None or current_body:
        messages.append((current_dt, "\n".join(current_body).strip("\n")))
    return messages


# ---------------------------------------------------------------------------
# Report extraction
# ---------------------------------------------------------------------------

# Each entry maps a logical field to a regex that matches the *first* line that
# holds that field. Both uppercase (Robinson) and title-case (Eduardo) headers
# are accepted because the regexes are case-insensitive. The patterns absorb:
#   * "SECTOR : : FASE 14"          (iOS double-colon)
#   * "Sector;  FASE 15"            (Android semicolon)
#   * "Fondo.   180"                (Robinson-style dot instead of colon)
#   * "Casing 8\": 6.0 m"           (RC drilling, diameter in the field name)
#   * "Casing de 14": 6,50"         (diameter with "de" prefix)
#   * "Fondo 13 3/4: 90.00 mts"     (drill-bit fraction in the field name;
#                                    the "(?:\s+(?:\d+\"?|\d+\s+\d+/\d+\"?))*"
#                                    fragment in fondo/avance/programado
#                                    lets us skip those bit-size annotations
#                                    and grab the actual numeric value).
FIELD_PATTERNS: dict[str, re.Pattern] = {
    "equipo":       re.compile(r"^\s*E?QUIPO\b[^\n:]*?[:;\-=\s]+(.+)$", re.IGNORECASE),
    "pozo":         re.compile(r"^\s*P[OÓ]ZO\b[^\n:]*?[:;\-=\s]+(.+)$",  re.IGNORECASE),
    "sector":       re.compile(r"^\s*SECTOR\b[^\n:]*?[:;\-=\s]+(.+)$",   re.IGNORECASE),
    "inclinacion":  re.compile(r"^\s*INCLINACI[OÓ]N\b[^\n:]*?[:;\-=\s]+(.+)$", re.IGNORECASE),
    "azimut":       re.compile(r"^\s*AZIMUT\b[^\n:]*?[:;\-=\s]+(.+)$",   re.IGNORECASE),
    "programado":   re.compile(r"^\s*PROGRAMADO\b(?:\s+(?:\d+\"?|\d+\s+\d+/\d+\"?))*[^\n:]*?[:;\-=\s]+(.+)$", re.IGNORECASE),
    "inicio":       re.compile(r"^\s*(?:INICIO\.?|DESDE)\b[^\n:]*?[:;\-=\s]+(.+)$", re.IGNORECASE),
    "fondo":        re.compile(r"^\s*(?:FONDO|HASTA)\.?\b(?:\s+(?:\d+\"?|\d+\s+\d+/\d+\"?))*[^\n:]*?[:;\-=\s]+(.+)$", re.IGNORECASE),
    "avance":       re.compile(r"^\s*(?:AVANCE|PERFORADO)\b(?:\s+(?:\d+\"?|\d+\s+\d+/\d+\"?))*[^\n:]*?[:;\-=\s]+(.+)$", re.IGNORECASE),
    "casing":       re.compile(r"^\s*CASING(?:\s+(?:DE\s+)?\d+\"?)?\b[^\n:]*?[:;\-=\s]+(.+)$", re.IGNORECASE),
}

OBS_HEADER_RE = re.compile(
    r"^\s*(?:OBSERVACIONES|OBS\.?)\s*[:\-]?\s*(.*)$",
    re.IGNORECASE,
)


def extract_report(body: str, msg_dt: Optional[datetime]) -> Optional[Report]:
    """Try to parse a report block from a message body. Returns None if the
    body does not look like a drilling report.

    The first non-empty match wins for each field, so a stray "Pozo
    Finalizado." comment that appears between the report and the Obs section
    cannot overwrite the real well identifier."""
    if not body:
        return None
    lines = [ln.rstrip() for ln in body.splitlines()]
    fields: dict[str, str] = {}
    obs_lines: list[str] = []
    in_obs = False

    for ln in lines:
        if not ln.strip():
            in_obs = False
            continue
        if OBS_HEADER_RE.match(ln):
            in_obs = True
            tail = OBS_HEADER_RE.match(ln).group(1).strip()
            if tail:
                obs_lines.append(tail)
            continue
        if in_obs:
            obs_lines.append(ln.strip())
            continue
        matched_key: Optional[str] = None
        matched_val: Optional[str] = None
        for key, pat in FIELD_PATTERNS.items():
            if key in fields:
                continue  # first match wins
            m = pat.match(ln)
            if m:
                matched_key = key
                matched_val = m.group(1).strip()
                break
        if matched_key is not None:
            fields[matched_key] = matched_val
        else:
            # Keep unrecognized lines as observation candidates.
            obs_lines.append(ln.strip())

    if "pozo" not in fields or "sector" not in fields:
        return None
    phase = normalize_phase(fields.get("sector", ""))
    if not phase:
        return None

    # Determine the operational date. We use the message timestamp (msg_dt)
    # as the source of truth because the body-embedded date ("información
    # DD.MM.YYYY") has frequent typos in this chat (e.g. a same-day día report
    # re-sent the next evening, or a body date off by one day). The shift text
    # in the body still tells us which shift the report covers.
    if msg_dt is not None:
        body_date: Optional[date] = msg_dt.date()
    else:
        body_date = None
    if body_date is None:
        md = BODY_DATE_RE.search("\n".join(lines))
        if md:
            d, mo, y = md.groups()
            yi = int(y) + 2000 if len(y) == 2 else int(y)
            try:
                body_date = date(yi, int(mo), int(d))
            except ValueError:
                body_date = None
    if body_date is None:
        return None

    # Shift detection
    sm = SHIFT_RE.search("\n".join(lines))
    if sm:
        shift = "Día" if sm.group(1).lower().startswith("d") else "Noche"
    elif msg_dt is not None:
        # Heuristic: morning = night shift report, evening = day shift report.
        shift = "Noche" if msg_dt.hour < 12 else "Día"
    else:
        shift = ""

    return Report(
        report_date=body_date,
        shift=shift,
        equipo=fields.get("equipo", "").strip(),
        pozo=normalize_well_id(fields.get("pozo", "").strip()),
        sector_raw=fields.get("sector", "").strip(),
        phase=phase,
        inclination=fields.get("inclinacion"),
        programado=parse_number(fields.get("programado", "")),
        inicio=parse_number(fields.get("inicio", "")),
        fondo=parse_number(fields.get("fondo", "")),
        avance=parse_number(fields.get("avance", "")),
        casing_hwt=parse_number(fields.get("casing", "")),
        observaciones=" ".join(obs_lines).strip(),
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def week_start(d: date) -> date:
    """Return the Monday of the week containing d (ISO week)."""
    return d - timedelta(days=d.weekday())


def month_label(d: date) -> str:
    return MONTH_LABELS[d.month - 1]


def dominant_phase(reports: list[Report]) -> str:
    """Pick the canonical phase for a well in a week.

    Rule: the phase with the most non-zero-avance records wins. Ties are broken
    by the phase that holds the deepest fondo (i.e. the completion phase). This
    matches the spec: "asignarlo a la fase donde se completó la perforación".
    """
    counts: Counter = Counter()
    deepest: dict[str, float] = {}
    for r in reports:
        if r.avance is not None and r.avance > 0:
            counts[r.phase] += 1
            if r.fondo is not None and deepest.get(r.phase, -1) < r.fondo:
                deepest[r.phase] = r.fondo
    if not counts:
        # Fallback: most-recent phase, then alphabetical
        return reports[-1].phase
    top = counts.most_common()
    max_count = top[0][1]
    candidates = [p for p, c in top if c == max_count]
    if len(candidates) == 1:
        return candidates[0]
    # Tie-break by deepest fondo, then by last appearance.
    candidates.sort(
        key=lambda p: (-deepest.get(p, 0.0),
                       max(i for i, r in enumerate(reports) if r.phase == p))
    )
    return candidates[0]


def aggregate(reports: list[Report]) -> tuple[list[WeeklyRow], list[str]]:
    """Group reports by week and compute phase totals + active wells."""
    by_week: dict[date, list[Report]] = defaultdict(list)
    for r in reports:
        by_week[week_start(r.report_date)].append(r)

    warnings: list[str] = []
    rows: list[WeeklyRow] = []

    for wk in sorted(by_week):
        items = by_week[wk]
        by_well: dict[str, list[Report]] = defaultdict(list)
        for r in items:
            by_well[r.pozo].append(r)

        phase_totals: dict[str, float] = defaultdict(float)
        active_wells: list[str] = []

        for pozo, well_reports in by_well.items():
            phases_present = {r.phase for r in well_reports}
            if len(phases_present) > 1:
                canonical = dominant_phase(well_reports)
                warnings.append(
                    f"Semana {wk}: {pozo} aparece en fases "
                    f"{[phase_label(p) for p in sorted(phases_present)]} — "
                    f"asignado a {phase_label(canonical)} (fase dominante)."
                )
            else:
                canonical = next(iter(phases_present))

            # Net advance from global min(inicio) / max(fondo) of all reports
            # of this well in the week. Falling back to Σ Perforado when the
            # depth values are missing.
            inicios = [r.inicio for r in well_reports if r.inicio is not None]
            fondos = [r.fondo for r in well_reports if r.fondo is not None]
            if inicios and fondos:
                net = max(fondos) - min(inicios)
            else:
                net = sum(r.avance or 0.0 for r in well_reports)
            # Guard against negative net (happens if fondo < inicio within
            # the same shift; treat as zero).
            if net < 0:
                net = sum(r.avance or 0.0 for r in well_reports)
            phase_totals[canonical] += net

            sum_avance = sum(r.avance or 0.0 for r in well_reports)
            if sum_avance > 0 and abs(net - sum_avance) > TOLERANCE_M:
                warnings.append(
                    f"Semana {wk}: {pozo} en {phase_label(canonical)} — "
                    f"avance neto {net:.2f} m vs Σ Perforado {sum_avance:.2f} m "
                    f"(Δ {net - sum_avance:+.2f} m)."
                )

            # Active-well markers
            started = False
            finished = False
            for r in well_reports:
                if r.inicio == 0 and r.fondo == 0 and (r.avance or 0) == 0:
                    # First shift in a brand-new well, no meters drilled yet.
                    started = True
                obs_low = r.observaciones.lower()
                if (r.avance is None or r.avance == 0) and any(
                    kw in obs_low for kw in FINISH_KEYWORDS
                ):
                    finished = True
            if started and finished:
                mark = "**"
            elif started:
                mark = "*"
            elif finished:
                mark = "**"
            else:
                mark = ""
            active_wells.append(f"{pozo}{mark}")

        rows.append(WeeklyRow(
            week_start=wk,
            month_label=month_label(wk),
            phases=dict(phase_totals),
            active_wells=sorted(active_wells),
            total=sum(phase_totals.values()),
        ))

    return rows, warnings


# ---------------------------------------------------------------------------
# Per-well aggregation
# ---------------------------------------------------------------------------

def summarize_wells(reports: list[Report]) -> list[WellSummary]:
    """Build a per-well summary from a list of reports.

    The list is sorted by ``total_m`` descending so the most productive
    wells come first. Wells with no reports are skipped (e.g. if the
    upstream filter was empty for that well)."""
    by_well: dict[str, list[Report]] = defaultdict(list)
    for r in reports:
        if r.pozo:
            by_well[r.pozo].append(r)

    summaries: list[WellSummary] = []
    for pozo, well_reports in by_well.items():
        phases = sorted({r.phase for r in well_reports})
        total_m = sum(r.avance or 0.0 for r in well_reports)
        inicios = [r.inicio for r in well_reports if r.inicio is not None]
        fondos = [r.fondo for r in well_reports if r.fondo is not None]
        casings = [r.casing_hwt for r in well_reports if r.casing_hwt is not None]
        dates = [r.report_date for r in well_reports]
        weeks = {week_start(r.report_date) for r in well_reports}
        num_shifts = len(well_reports)
        num_active = sum(1 for r in well_reports if (r.avance or 0) > 0)

        # 'Started' = at least one record at 0/0 with no avance (well begun
        # within the filtered period). 'Finished' = at least one zero-avance
        # record with a finalize/disequip/relocate keyword in observations.
        has_started = any(
            r.inicio == 0 and r.fondo == 0 and (r.avance or 0) == 0
            for r in well_reports
        )
        has_finished = any(
            (r.avance is None or r.avance == 0)
            and any(kw in r.observaciones.lower() for kw in FINISH_KEYWORDS)
            for r in well_reports
        )

        summaries.append(WellSummary(
            pozo=pozo,
            phases=phases,
            total_m=total_m,
            inicio=min(inicios) if inicios else 0.0,
            fondo=max(fondos) if fondos else 0.0,
            avg_m_per_shift=(total_m / num_shifts) if num_shifts else 0.0,
            casing_total=max(casings) if casings else 0.0,
            num_shifts=num_shifts,
            num_active_shifts=num_active,
            num_weeks=len(weeks),
            first_date=min(dates) if dates else None,
            last_date=max(dates) if dates else None,
            has_started=has_started,
            has_finished=has_finished,
        ))

    summaries.sort(key=lambda w: w.total_m, reverse=True)
    return summaries


def wells_by_phase_meters(reports: list[Report]) -> dict[tuple[str, str], float]:
    """For each ``(pozo, phase)`` pair, sum the ``Perforado`` over the given
    reports. Returns a dict keyed by ``(pozo, phase)`` with total metres as
    the value. Missing pairs (well never operated in that phase) are simply
    absent from the dict.

    This is the raw per-shift attribution: a well that relocated between
    phases (e.g. ZD-2833 alternating F12/F13) will have entries in both
    columns, with the actual metres drilled in each. It is *not* collapsed
    to the dominant phase (that's the weekly table's job).
    """
    matrix: dict[tuple[str, str], float] = defaultdict(float)
    for r in reports:
        if r.pozo and (r.avance or 0) > 0:
            matrix[(r.pozo, r.phase)] += r.avance
    return dict(matrix)


# ---------------------------------------------------------------------------
# Strict per-period aggregation (daily / monthly)
# ---------------------------------------------------------------------------

def aggregate_by_period(
    reports: list[Report],
    period: str = "month",
) -> list[PeriodRow]:
    """Strict per-period aggregation (no net advance).

    ``period="month"`` groups reports by their calendar month;
    ``period="day"`` keeps each calendar day as its own row. Empty
    periods (days or months with no reports) are *not* emitted — only
    periods that actually had drilling activity appear in the result.

    Each ``PeriodRow.total`` is the literal sum of ``Perforado`` over the
    reports in that period. Summing all ``total`` values equals
    ``Σ Perforado`` of the input — i.e. the raw total, without the
    "stretched-across-the-boundary" effect that ``WeeklyRow`` has when a
    well crosses a month or week boundary.
    """
    if period == "month":
        def _key(d: date) -> date:
            return date(d.year, d.month, 1)
        def _label(d: date) -> str:
            return f"{MONTH_LABELS[d.month - 1]} {d.year}"
    elif period == "day":
        def _key(d: date) -> date:
            return d
        def _label(d: date) -> str:
            return d.strftime("%d/%m/%y")
    else:
        raise ValueError(f"period must be 'month' or 'day', got {period!r}")

    bucket: dict[date, dict] = defaultdict(
        lambda: {"phases": defaultdict(float), "wells": set()}
    )
    for r in reports:
        if (r.avance or 0) <= 0:
            continue
        ps = _key(r.report_date)
        bucket[ps]["phases"][r.phase] += r.avance
        bucket[ps]["wells"].add((r.pozo, r.shift))

    rows: list[PeriodRow] = []
    for ps in sorted(bucket):
        data = bucket[ps]
        # Deduplicate wells (a well with 3 shifts in the month appears once).
        unique_wells = sorted({w[0] for w in data["wells"]})
        rows.append(PeriodRow(
            period_start=ps,
            period_label=_label(ps),
            phases=dict(data["phases"]),
            active_wells=unique_wells,
            total=sum(data["phases"].values()),
        ))
    return rows


# ---------------------------------------------------------------------------
# Per-month subtotals
# ---------------------------------------------------------------------------

def monthly_totals(rows: list[WeeklyRow]) -> list[tuple[str, dict[str, float], float]]:
    """Return [(month_label, {phase: m}, total), ...] sorted chronologically
    (by the first week_start of each month)."""
    by_month: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    first_seen: dict[str, date] = {}
    for r in rows:
        if r.month_label not in first_seen or r.week_start < first_seen[r.month_label]:
            first_seen[r.month_label] = r.week_start
        for p, v in r.phases.items():
            by_month[r.month_label][p] += v
    out: list[tuple[str, dict[str, float], float]] = []
    for label in sorted(first_seen, key=lambda k: first_seen[k]):
        phases = dict(by_month[label])
        out.append((label, phases, sum(phases.values())))
    return out


# ---------------------------------------------------------------------------
# Output: CSV and Markdown
# ---------------------------------------------------------------------------

def build_csv(rows: list[WeeklyRow], path: Path,
               phases: Optional[list[str]] = None) -> None:
    phases = phases or PHASE_KEYS
    headers = (
        ["Mes - Semana"]
        + [f"{phase_label(k)} (m)" for k in phases]
        + ["Pozo(s) en ejecución", "Total Semanal (m)"]
    )
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in rows:
            iso_wk = row.week_start.isocalendar().week
            cells = [f"{row.month_label} - S{iso_wk:02d}"]
            for k in phases:
                v = row.phases.get(k)
                cells.append(f"{v:.2f}" if v else "–")
            cells.append(", ".join(row.active_wells) if row.active_wells else "–")
            cells.append(f"{row.total:.2f}")
            writer.writerow(cells)
        # Per-month subtotals
        for label, monthly, total in monthly_totals(rows):
            cells = [f"TOTAL {label.upper()}"]
            for k in phases:
                v = monthly.get(k)
                cells.append(f"{v:.2f}" if v else "–")
            cells.append("")
            cells.append(f"{total:.2f}")
            writer.writerow(cells)
        # Grand total
        totals = {k: sum(r.phases.get(k, 0.0) for r in rows) for k in phases}
        grand = sum(totals.values())
        cells = ["TOTAL ACUMULADO"]
        for k in phases:
            cells.append(f"{totals[k]:.2f}" if totals[k] else "–")
        cells.append("")
        cells.append(f"{grand:.2f}")
        writer.writerow(cells)


def build_markdown(rows: list[WeeklyRow], warnings: list[str],
                   report_count: int,
                   phases: Optional[list[str]] = None) -> str:
    phases = phases or PHASE_KEYS
    headers = (
        ["Mes - Semana"]
        + [f"{phase_label(k)} (m)" for k in phases]
        + ["Pozo(s) en ejecución", "Total Semanal (m)"]
    )
    lines: list[str] = []
    lines.append("# Resumen Semanal de Perforación")
    lines.append("")
    lines.append(f"_Reportes diarios procesados: **{report_count}**_")
    lines.append("")
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        iso_wk = row.week_start.isocalendar().week
        cells = [f"{row.month_label} - S{iso_wk:02d}"]
        for k in phases:
            v = row.phases.get(k)
            cells.append(f"{v:.2f}" if v else "–")
        cells.append("<br>".join(row.active_wells) if row.active_wells else "–")
        cells.append(f"{row.total:.2f}")
        lines.append("| " + " | ".join(cells) + " |")
    # Grand total row
    totals = {k: sum(r.phases.get(k, 0.0) for r in rows) for k in phases}
    grand = sum(totals.values())
    cells = ["**TOTAL**"]
    for k in phases:
        cells.append(f"**{totals[k]:.2f}**" if totals[k] else "–")
    cells.append("")
    cells.append(f"**{grand:.2f}**")
    lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    # Per-month breakdown
    if len({r.month_label for r in rows}) > 1:
        lines.append("## Totales por mes")
        lines.append("")
        lines.append("| Mes | " + " | ".join(phase_label(k) for k in phases) + " | Total (m) |")
        lines.append("|" + "|".join(["---"] * (len(phases) + 2)) + "|")
        for label, monthly, total in monthly_totals(rows):
            cells = [label]
            for k in phases:
                v = monthly.get(k)
                cells.append(f"{v:.2f}" if v else "–")
            cells.append(f"{total:.2f}")
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    # Technical notes
    lines.append("## 📌 Notas técnicas")
    lines.append("")
    lines.append(
        "- **Fases normalizadas** a `FASE_NN`. Variantes como `Fase 14`, "
        "`FASE 8 sur`, `FASE 8S`, `FASE 8 S` y `FASE 09` se canonicalizan "
        "automáticamente."
    )
    lines.append(
        "- **Reubicaciones de plataforma** (p. ej. `ZD-2833`, que alterna "
        "entre Fase 12 y Fase 13 entre turnos): se elige la fase con más "
        "registros de avance no nulo; en empate, la fase donde el pozo alcanza "
        "mayor profundidad final. Todos los metros semanales del pozo se "
        "asignan a esa fase canónica."
    )
    lines.append(
        "- **Avance semanal por fase** = `max(Fondo) − min(Inicio)` "
        "considerando todos los turnos del mismo pozo dentro de la semana "
        "(lunes a domingo, semana ISO). Si faltan `Desde`/`Hasta`, se usa "
        "`Σ Perforado` como respaldo."
    )
    lines.append(
        "- **Decimales**: el parser acepta coma o punto como separador "
        "(`115,00 mts` ≡ `115.00 mts`); se ignoran unidades (`m`, `mts`, `mt`, "
        "`°`, etc.)."
    )
    lines.append(
        "- **Marcadores en `Pozo(s) en ejecución`**: `*` indica pozo que "
        "inició perforación en la semana (turno de instalación, "
        "`inicio = fondo = 0`); `**` indica pozo finalizado/desarmado/"
        "trasladado en la semana. Un mismo pozo puede llevar ambos en la "
        "misma semana."
    )
    lines.append(
        "- **Celdas vacías** se muestran como `–`."
    )
    lines.append(
        f"- **Tolerancia de validación**: ±{TOLERANCE_M} m entre avance neto "
        "y suma de perforado. Las discrepancias se listan en la sección de "
        "advertencias."
    )

    if warnings:
        lines.append("")
        lines.append("## ⚠️ Advertencias de validación")
        for w in warnings:
            lines.append(f"- {w}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Validation & self-tests
# ---------------------------------------------------------------------------

def validate_reports(reports: list[Report]) -> list[str]:
    """Per-report sanity checks: depth consistency, and (pozo, date, shift)
    duplicates whose depth ranges overlap (real duplicates). Non-overlapping
    records that share the same label are treated as sequential shifts, not
    duplicates, and left for the aggregator to handle."""
    issues: list[str] = []
    # Group by (pozo, date, shift)
    groups: dict[tuple, list[Report]] = defaultdict(list)
    for r in reports:
        groups[(r.pozo, r.report_date, r.shift)].append(r)
    for key, group in groups.items():
        if len(group) > 1:
            # Keep records with at least one depth value, then check overlap.
            with_depths = [r for r in group
                           if r.inicio is not None or r.fondo is not None]
            overlap = False
            for i in range(len(with_depths)):
                for j in range(i + 1, len(with_depths)):
                    a, b = with_depths[i], with_depths[j]
                    a_start, a_end = a.inicio, a.fondo
                    b_start, b_end = b.inicio, b.fondo
                    if a_start is None or a_end is None or b_start is None or b_end is None:
                        continue
                    if max(a_start, b_start) < min(a_end, b_end) - TOLERANCE_M:
                        overlap = True
                        break
                if overlap:
                    break
            if overlap:
                issues.append(
                    f"Duplicado: {key[0]} {key[1]} turno {key[2]} — rangos de "
                    f"profundidad solapados entre {len(with_depths)} registros"
                )
        for r in group:
            if r.inicio is not None and r.fondo is not None and r.avance is not None:
                delta = (r.fondo - r.inicio) - r.avance
                if abs(delta) > TOLERANCE_M:
                    issues.append(
                        f"{r.pozo} {r.report_date} {r.shift}: "
                        f"Fondo−Inicio={r.fondo - r.inicio:.2f} m vs "
                        f"Avance={r.avance:.2f} m (Δ {delta:+.2f} m)"
                    )
    return issues


def self_test(reports: list[Report], rows: list[WeeklyRow]) -> list[str]:
    """Aggregate-level checks required by the spec:
       * Σ(Fases) = Total Semanal
       * No overlapping (pozo, fecha, turno) — i.e. no real duplicate shifts.
    """
    issues: list[str] = []
    for row in rows:
        phase_sum = sum(row.phases.values())
        if abs(phase_sum - row.total) > 0.05:
            issues.append(
                f"Semana {row.week_start}: Σ(Fases)={phase_sum:.2f} m "
                f"≠ Total={row.total:.2f} m"
            )
    # Detect overlapping (pozo, date, shift) — real duplicates.
    groups: dict[tuple, list[Report]] = defaultdict(list)
    for r in reports:
        groups[(r.pozo, r.report_date, r.shift)].append(r)
    for key, group in groups.items():
        if len(group) <= 1:
            continue
        with_depths = [r for r in group
                       if r.inicio is not None and r.fondo is not None]
        for i in range(len(with_depths)):
            for j in range(i + 1, len(with_depths)):
                a, b = with_depths[i], with_depths[j]
                if max(a.inicio, b.inicio) < min(a.fondo, b.fondo) - 0.05:
                    issues.append(f"Pozo duplicado (rangos solapados): {key}")
                    break
    return issues


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_all(text: str) -> tuple[list[Report], list[str]]:
    messages = split_messages(text)
    reports: list[Report] = []
    failures: list[str] = []
    for dt, body in messages:
        r = extract_report(body, dt)
        if r is not None:
            reports.append(r)
        elif any(kw in body.lower() for kw in ("pozo", "equipo", "sector")):
            failures.append(body.splitlines()[0][:80] if body else "")
    return reports, failures


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analizador semanal de reportes de perforación diamantina."
    )
    parser.add_argument("--input", "-i", default="_chat.txt",
                        help="Archivo de chat WhatsApp (por defecto: _chat.txt)")
    parser.add_argument("--csv", default="avance_semanal.csv",
                        help="Salida CSV (por defecto: avance_semanal.csv)")
    parser.add_argument("--md",  default="reporte.md",
                        help="Salida Markdown (por defecto: reporte.md)")
    parser.add_argument("--tests", action="store_true",
                        help="Solo ejecutar validaciones, sin escribir archivos")
    args = parser.parse_args(argv)

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"ERROR: archivo de entrada no encontrado: {in_path}",
              file=sys.stderr)
        return 1

    try:
        text = in_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"ERROR: no se pudo leer {in_path}: {exc}", file=sys.stderr)
        return 1

    reports, failures = parse_all(text)
    if not reports:
        print("ERROR: no se parseó ningún reporte. "
              "Verifica que el archivo sea un chat con bloques de reporte.",
              file=sys.stderr)
        return 1

    issues = validate_reports(reports)
    rows, agg_warnings = aggregate(reports)
    test_issues = self_test(reports, rows)

    if args.tests:
        print(f"Reportes parseados:    {len(reports)}")
        print(f"Mensajes no reconocidos: {len(failures)}")
        for f in failures[:5]:
            print(f"  - {f!r}")
        print(f"Advertencias de validación: {len(issues)}")
        for i in issues[:10]:
            print(f"  - {i}")
        print(f"Advertencias de agregación: {len(agg_warnings)}")
        for w in agg_warnings[:10]:
            print(f"  - {w}")
        print(f"Fallos de self-test:    {len(test_issues)}")
        for t in test_issues:
            print(f"  - {t}")
        return 0 if not (issues or test_issues) else 2

    csv_path = Path(args.csv)
    md_path = Path(args.md)
    build_csv(rows, csv_path)
    md_text = build_markdown(rows, agg_warnings + issues, len(reports))
    md_path.write_text(md_text, encoding="utf-8")

    print(md_text)
    print(f"CSV:  {csv_path}")
    print(f"MD:   {md_path}")
    if issues or agg_warnings:
        print(f"({len(issues) + len(agg_warnings)} nota(s) de validación — ver reporte)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
