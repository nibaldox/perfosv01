"""Streamlit UI for the diamond-drilling weekly analyzer.

Run locally:
    pip install -r requirements.txt
    streamlit run app.py

Deploy: push the repo to GitHub and connect it to https://share.streamlit.io
(Streamlit Community Cloud). Main file: app.py.
"""
from __future__ import annotations

import sys
import tempfile
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Make the sibling script.py importable when launched from any cwd.
sys.path.insert(0, str(Path(__file__).parent))
from script import (  # noqa: E402
    PHASE_KEYS,
    PHASE_LABELS,
    Report,
    WeeklyRow,
    WellSummary,
    aggregate,
    build_csv,
    build_markdown,
    parse_all,
    phase_label,
    present_phases,
    self_test,
    summarize_wells,
    validate_reports,
    week_start,
)

st.set_page_config(
    page_title="Perforación Diamantina",
    page_icon="⛏️",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Procesando chat…")
def load_and_process(text: Optional[str]) -> Optional[dict]:
    """Parse chat text and aggregate by week. Cached by content hash.

    Returns ``None`` if no text was provided or no reports could be extracted.
    """
    if not text:
        return None
    reports, parse_failures = parse_all(text)
    if not reports:
        return None
    issues = validate_reports(reports)
    rows, agg_warnings = aggregate(reports)
    return {
        "reports": reports,
        "failures": parse_failures,
        "issues": issues,
        "rows": rows,
        "warnings": agg_warnings,
        "phases": present_phases(reports),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_uploaded_chat(uploaded) -> tuple[Optional[str], str]:
    """Extract the chat text from a file uploaded via ``st.file_uploader``.

    Accepts either a plain ``.txt`` (WhatsApp Android export, or WhatsApp Web
    "without media") or a ``.zip`` (WhatsApp Web "include media" export).
    Returns ``(text, info)`` where ``text`` is the chat contents and
    ``info`` is a short human-readable description shown to the user.

    On error, ``text`` is ``None`` and ``info`` is the error message.
    """
    name = uploaded.name
    raw = uploaded.read()
    if name.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(BytesIO(raw)) as zf:
                # Find every non-directory .txt entry (top level or nested).
                txt_members = [
                    n for n in zf.namelist()
                    if not n.endswith("/") and n.lower().endswith(".txt")
                ]
                if not txt_members:
                    return None, (
                        f"📦 El ZIP `{name}` no contiene ningún archivo `.txt`."
                    )
                # If several .txt files exist, prefer the largest (the chat
                # is almost always the biggest one — README files etc. are
                # tiny). Stable tiebreak by name.
                txt_members.sort(
                    key=lambda n: (-zf.getinfo(n).file_size, n)
                )
                chosen = txt_members[0]
                extra_txt = (
                    f" — se ignoraron {len(txt_members) - 1} .txt adicionales"
                    if len(txt_members) > 1 else ""
                )
                with zf.open(chosen) as fh:
                    text = fh.read().decode("utf-8", errors="replace")
                media_count = sum(
                    1 for n in zf.namelist()
                    if not n.endswith("/") and not n.lower().endswith(".txt")
                )
                media_info = (
                    f", {media_count} archivos multimedia ignorados"
                    if media_count else ""
                )
                return text, (
                    f"📦 ZIP `{name}` → `{chosen}` "
                    f"({len(text):,} chars{media_info}){extra_txt}"
                )
        except zipfile.BadZipFile:
            return None, f"El archivo `{name}` no es un ZIP válido."
        except Exception as exc:  # pragma: no cover — defensive
            return None, f"No se pudo leer el ZIP `{name}`: {exc}"
    # Plain text fallback.
    text = raw.decode("utf-8", errors="replace")
    return text, f"📄 `{name}` ({len(text):,} chars)"


def filter_reports(
    reports: list[Report],
    phases: list[str],
    wells: list[str],
    date_range: tuple[date, date],
) -> list[Report]:
    out = []
    for r in reports:
        if r.phase not in phases:
            continue
        if r.pozo not in wells:
            continue
        if not (date_range[0] <= r.report_date <= date_range[1]):
            continue
        out.append(r)
    return out


def build_weekly_df(rows: list[WeeklyRow], phases: list[str]) -> pd.DataFrame:
    headers = (
        ["Mes - Semana"]
        + [f"{phase_label(k)} (m)" for k in phases]
        + ["Pozo(s) en ejecución", "Total Semanal (m)"]
    )
    data = []
    for r in rows:
        iso_wk = r.week_start.isocalendar().week
        cells = [f"{r.month_label} - S{iso_wk:02d}"]
        for k in phases:
            v = r.phases.get(k)
            cells.append(round(v, 2) if v else None)
        cells.append(", ".join(r.active_wells) if r.active_wells else None)
        cells.append(round(r.total, 2))
        data.append(cells)
    return pd.DataFrame(data, columns=headers)


def stacked_bar_chart(rows: list[WeeklyRow], phases: list[str]) -> go.Figure:
    fig = go.Figure()
    if not rows:
        return fig
    weeks = [f"{r.month_label} S{r.week_start.isocalendar().week:02d}" for r in rows]
    for k in phases:
        vals = [r.phases.get(k, 0.0) for r in rows]
        if any(v > 0 for v in vals):
            label = phase_label(k)
            fig.add_trace(go.Bar(name=label, x=weeks, y=vals,
                                 hovertemplate="%{x}<br>" + label +
                                               ": %{y:.2f} m<extra></extra>"))
    fig.update_layout(
        barmode="stack",
        title="Avance semanal por fase",
        xaxis_title="Semana",
        yaxis_title="Metros perforados (m)",
        legend_title="Fase",
        height=420,
    )
    return fig


def gantt_chart(reports: list[Report]) -> go.Figure:
    fig = go.Figure()
    wells = sorted({r.pozo for r in reports})
    if not wells:
        return fig
    spans = []
    for w in wells:
        dates = [r.report_date for r in reports if r.pozo == w]
        phases = {r.phase for r in reports if r.pozo == w}
        phase = sorted(phases, key=lambda p: sum(
            1 for r in reports if r.pozo == w and r.phase == p
            and (r.avance or 0) > 0), reverse=True)[0]
        spans.append({
            "Pozo": w,
            "Inicio": min(dates),
            "Fin": max(dates) + timedelta(days=1),
            "Fase": PHASE_LABELS.get(phase, phase),
        })
    df = pd.DataFrame(spans)
    fig = px.timeline(
        df, x_start="Inicio", x_end="Fin", y="Pozo", color="Fase",
        title="Línea de tiempo de pozos",
    )
    fig.update_yaxes(autorange="reversed")
    fig.update_layout(height=max(280, 38 * len(spans) + 80))
    return fig


def cumulative_chart(rows: list[WeeklyRow], phases: list[str]) -> go.Figure:
    fig = go.Figure()
    if not rows:
        return fig
    weeks = [f"{r.month_label} S{r.week_start.isocalendar().week:02d}" for r in rows]
    for k in phases:
        cumulative = []
        total = 0.0
        for r in rows:
            total += r.phases.get(k, 0.0)
            cumulative.append(total)
        if any(v > 0 for v in cumulative):
            label = phase_label(k)
            fig.add_trace(go.Scatter(
                name=label, x=weeks, y=cumulative,
                mode="lines+markers",
                hovertemplate="%{x}<br>" + label +
                              ": %{y:.2f} m<extra></extra>",
            ))
    fig.update_layout(
        title="Acumulado por fase",
        xaxis_title="Semana",
        yaxis_title="Metros acumulados (m)",
        legend_title="Fase",
        height=420,
    )
    return fig


def rows_to_csv(rows: list[WeeklyRow], phases: list[str]) -> str:
    """Reuse script.build_csv via a temp file so the export matches the CLI."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as fh:
        tmp = Path(fh.name)
    try:
        build_csv(rows, tmp, phases=phases)
        return tmp.read_text(encoding="utf-8")
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Per-well helpers
# ---------------------------------------------------------------------------

def _primary_phase(w: WellSummary) -> str:
    """Phase used for color coding in the per-well charts.

    Picks the phase with the largest contribution to ``w.total_m`` (uses
    simple report counts as a proxy — the report count and total_m correlate
    closely enough for color-bucketing purposes and avoids a second pass
    through the report list)."""
    return w.phases[0] if w.phases else "OTHER"


def build_well_summary_df(per_well: list[WellSummary]) -> pd.DataFrame:
    """Build the per-well summary table shown in the UI."""
    rows = []
    for w in per_well:
        if w.has_started and w.has_finished:
            estado = "*** Iniciado y finalizado"
        elif w.has_started:
            estado = "* Iniciado"
        elif w.has_finished:
            estado = "** Finalizado"
        else:
            estado = "—"

        fases_str = ", ".join(phase_label(p) for p in w.phases)
        depth = f"{w.inicio:.2f} → {w.fondo:.2f}"
        if w.first_date and w.last_date:
            if w.first_date == w.last_date:
                periodo = w.first_date.strftime("%d/%m/%y")
            else:
                periodo = (
                    f"{w.first_date.strftime('%d/%m/%y')} → "
                    f"{w.last_date.strftime('%d/%m/%y')}"
                )
        else:
            periodo = "—"

        rows.append({
            "Pozo": w.pozo,
            "Fases": fases_str,
            "Total (m)": round(w.total_m, 2),
            "Desde → Hasta (m)": depth,
            "Avg m/turno": round(w.avg_m_per_shift, 2),
            "Casing (m)": round(w.casing_total, 2),
            "# Turnos": w.num_shifts,
            "Semanas": w.num_weeks,
            "Período": periodo,
            "Estado": estado,
        })
    return pd.DataFrame(rows)


def top_wells_chart(per_well: list[WellSummary], top_n: int = 15) -> go.Figure:
    """Horizontal bar chart of the top N wells by total meters, color-coded
    by primary phase. Wells with the same primary phase are stacked under a
    single legend entry."""
    fig = go.Figure()
    sorted_wells = sorted(per_well, key=lambda w: w.total_m, reverse=True)[:top_n]
    if not sorted_wells:
        return fig
    # Group by primary phase to build one bar trace per phase.
    for phase_key in sorted({_primary_phase(w) for w in sorted_wells},
                            key=lambda p: phase_label(p)):
        ws = [w for w in sorted_wells if _primary_phase(w) == phase_key]
        fig.add_trace(go.Bar(
            name=phase_label(phase_key),
            y=[w.pozo for w in ws],
            x=[w.total_m for w in ws],
            orientation="h",
            hovertemplate=(
                "<b>%{y}</b><br>" + phase_label(phase_key)
                + ": %{x:.2f} m<extra></extra>"
            ),
        ))
    fig.update_layout(
        barmode="group",
        title=f"Top {len(sorted_wells)} pozos por metros perforados",
        xaxis_title="Total (m)",
        yaxis_title="Pozo",
        yaxis=dict(autorange="reversed"),  # largest at top
        height=max(320, 30 * len(sorted_wells) + 90),
    )
    return fig


def efficiency_scatter(per_well: list[WellSummary]) -> go.Figure:
    """Scatter: total m vs avg m/turno. Bubble size = # turnos. Color =
    primary phase. Useful for spotting inefficient or over-performing wells."""
    fig = go.Figure()
    if not per_well:
        return fig
    for phase_key in sorted({_primary_phase(w) for w in per_well},
                            key=lambda p: phase_label(p)):
        ws = [w for w in per_well if _primary_phase(w) == phase_key]
        fig.add_trace(go.Scatter(
            name=phase_label(phase_key),
            x=[w.total_m for w in ws],
            y=[w.avg_m_per_shift for w in ws],
            mode="markers",
            marker=dict(
                size=[max(10, min(45, w.num_shifts * 2.2)) for w in ws],
                sizemode="diameter",
                line=dict(width=1, color="white"),
                opacity=0.85,
            ),
            text=[
                f"{w.pozo}<br>{w.num_shifts} turnos<br>{w.num_active_shifts} con avance"
                for w in ws
            ],
            hovertemplate=(
                "%{text}<br>Total: %{x:.1f} m<br>Avg: %{y:.2f} m/turno<extra></extra>"
            ),
        ))
    fig.update_layout(
        title="Eficiencia por pozo: total vs avg m/turno",
        xaxis_title="Total perforado (m)",
        yaxis_title="Avg m/turno (m/shift)",
        height=440,
    )
    return fig


def activity_heatmap(per_well: list[WellSummary],
                     reports: list[Report]) -> go.Figure:
    """Heatmap: well × ISO-week, color = meters that week.

    The y-axis is sorted by the well's first appearance in the data (so
    chronological campaigns read top-to-bottom). Empty cells (no reports
    that week) are coloured at the bottom of the scale."""
    fig = go.Figure()
    if not per_well or not reports:
        return fig
    # Build (well, week) -> meters
    cells: dict[tuple[str, date], float] = defaultdict(float)
    all_weeks: set[date] = set()
    well_first_seen: dict[str, date] = {}
    for r in reports:
        if not r.pozo:
            continue
        wk = week_start(r.report_date)
        if (r.avance or 0) > 0:
            cells[(r.pozo, wk)] += r.avance
        all_weeks.add(wk)
        if r.pozo not in well_first_seen or r.report_date < well_first_seen[r.pozo]:
            well_first_seen[r.pozo] = r.report_date

    wells = sorted(
        {w.pozo for w in per_well},
        key=lambda p: (well_first_seen.get(p, date(2099, 1, 1)), p),
    )
    weeks_sorted = sorted(all_weeks)
    z = [[cells.get((w, wk), 0.0) for wk in weeks_sorted] for w in wells]
    fig.add_trace(go.Heatmap(
        z=z,
        x=[f"S{wk.isocalendar().week:02d}" for wk in weeks_sorted],
        y=wells,
        colorscale="YlOrRd",
        hovertemplate=(
            "Pozo %{y}<br>Semana %{x}<br>%{z:.2f} m<extra></extra>"
        ),
        colorbar=dict(title="m"),
    ))
    fig.update_layout(
        title="Actividad por pozo × semana (m perforados)",
        xaxis_title="Semana ISO",
        yaxis_title="Pozo",
        height=max(320, 24 * len(wells) + 90),
    )
    return fig


def well_detail_view(well: str, reports: list[Report]) -> tuple[pd.DataFrame, go.Figure]:
    """Drilldown for a single well: per-week, per-phase table + a stacked
    line chart of cumulative m per phase over time."""
    well_reports = [r for r in reports if r.pozo == well]
    # Per-week per-phase accumulation
    by_week: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in well_reports:
        if (r.avance or 0) > 0:
            by_week[week_start(r.report_date)][r.phase] += r.avance
    weeks_sorted = sorted(by_week)
    if not weeks_sorted:
        return pd.DataFrame(), go.Figure()

    # Collect every phase this well operated in (preserve insertion order).
    all_phases: list[str] = []
    for r in well_reports:
        if r.phase not in all_phases:
            all_phases.append(r.phase)

    # Table rows
    table_rows = []
    for wk in weeks_sorted:
        row = {"Semana": wk.strftime("%d/%m/%y")}
        total = 0.0
        for p in all_phases:
            v = by_week[wk].get(p, 0.0)
            row[phase_label(p)] = round(v, 2)
            total += v
        row["Total semana"] = round(total, 2)
        table_rows.append(row)
    df = pd.DataFrame(table_rows)

    # Stacked area chart: cumulative m per phase over the weeks
    fig = go.Figure()
    for p in all_phases:
        cum: list[float] = []
        running = 0.0
        for wk in weeks_sorted:
            running += by_week[wk].get(p, 0.0)
            cum.append(running)
        fig.add_trace(go.Scatter(
            name=phase_label(p),
            x=[wk.strftime("%d/%m/%y") for wk in weeks_sorted],
            y=cum,
            mode="lines+markers",
            stackgroup="one",
            hovertemplate=(
                "%{x}<br>" + phase_label(p) + ": %{y:.2f} m<extra></extra>"
            ),
        ))
    fig.update_layout(
        title=f"Evolución semanal de {well}",
        xaxis_title="Semana",
        yaxis_title="Acumulado (m)",
        height=360,
    )
    return df, fig


def build_wells_phase_pivot(per_well: list[WellSummary],
                             reports: list[Report],
                             phases: list[str]) -> pd.DataFrame:
    """Build the wells × phases pivot table shown in the UI.

    Each row is a well (in the same order as the per-well summary, i.e.
    sorted by total metres descending). Each column is a phase, with the
    metres drilled in that phase as the cell value. Wells that never
    operated in a phase show an empty cell (not zero) so the reader can
    tell at a glance which combinations are real. A ``Total (m)`` column
    re-states the per-well total for cross-checking, and a bold ``TOTAL``
    row at the bottom sums each column.

    This is the "raw" per-shift attribution: a well that was reubicated
    between phases (e.g. ZD-2833 alternating F12/F13) will appear in both
    columns, with the actual metres drilled in each. It is *not* collapsed
    to the dominant phase.
    """
    from script import wells_by_phase_meters  # local import to keep top tidy
    matrix = wells_by_phase_meters(reports)
    rows = []
    for w in per_well:
        row = {"Pozo": w.pozo}
        for p in phases:
            v = matrix.get((w.pozo, p), 0.0)
            row[phase_label(p)] = round(v, 2) if v else None
        row["Total (m)"] = round(w.total_m, 2)
        rows.append(row)
    df = pd.DataFrame(rows)

    # Append a TOTAL row that sums each numeric column
    totals: dict[str, object] = {"Pozo": "**TOTAL**"}
    for p in phases:
        col = phase_label(p)
        vals = [r[col] for r in rows if isinstance(r.get(col), (int, float))]
        totals[col] = round(sum(vals), 2) if vals else None
    grand = sum(w.total_m for w in per_well)
    totals["Total (m)"] = round(grand, 2)
    df_with_total = pd.concat([df, pd.DataFrame([totals])], ignore_index=True)
    return df_with_total


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def main() -> None:
    """Render the Streamlit UI. Wrapping in a function means importing this
    module (e.g. for unit tests) does not trigger widget calls, which would
    raise outside a session context."""

    st.title("⛏️ Resumen Semanal — Perforación")
    st.caption(
        "Cargá el chat de WhatsApp con los reportes diarios y obtené el avance "
        "semanal por fase, con tabla, gráficos y exportación."
    )

    # --- Sidebar: data source ------------------------------------------------
    with st.sidebar:
        st.header("📂 Datos")
        uploaded = st.file_uploader(
            "Archivo _chat.txt o .zip de WhatsApp",
            type=["txt", "zip"],
        )

        if uploaded is not None:
            text, info = _read_uploaded_chat(uploaded)
            if text is None:
                st.error(info)
                text = None
            else:
                st.success(info)
        else:
            text = None

    if not text:
        st.warning("Subí un `.txt` desde la barra lateral para empezar.")
        try:
            st.stop()
        except Exception:
            return  # imported outside a session, nothing more to render

    data = load_and_process(text)
    if data is None:
        st.error(
            "No se pudo parsear ningún reporte del archivo. Verificá que sea "
            "un chat con bloques `Pozo:` / `Sector:` / `Desde:` / `Hasta:`."
        )
        try:
            st.stop()
        except Exception:
            return  # imported outside a session — nothing more to render
        return  # belt-and-suspenders: st.stop() may not actually stop in tests

    reports_all = data["reports"]
    rows_all = data["rows"]
    phases_all = data["phases"]

    # --- Sidebar: filters ----------------------------------------------------
    with st.sidebar:
        st.header("🔍 Filtros")
        sel_phases = st.multiselect(
            "Fases / Sectores",
            phases_all,
            default=phases_all,
            format_func=phase_label,
        )
        all_wells = sorted({r.pozo for r in reports_all if r.pozo})
        sel_wells = st.multiselect("Pozos", all_wells, default=all_wells)
        min_d = min(r.report_date for r in reports_all)
        max_d = max(r.report_date for r in reports_all)
        date_range = st.date_input(
            "Rango de fechas",
            value=(min_d, max_d),
            min_value=min_d,
            max_value=max_d,
        )
        if isinstance(date_range, date):
            date_range = (date_range, date_range)

    if not sel_phases or not sel_wells:
        st.warning("Seleccioná al menos una fase y un pozo.")
        try:
            st.stop()
        except Exception:
            return

    filtered = filter_reports(reports_all, sel_phases, sel_wells, date_range)
    if not filtered:
        st.warning("Ningún reporte matchea los filtros. Ajustá los criterios.")
        try:
            st.stop()
        except Exception:
            return
    filtered_rows, _ = aggregate(filtered)
    # Phase list for the filtered view: same ordering as the full dataset,
    # intersected with what the filtered reports actually use.
    filtered_phases = [k for k in phases_all
                       if any(r.phases.get(k, 0) for r in filtered_rows)]

    # --- KPIs ---------------------------------------------------------------
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total perforado", f"{sum(r.total for r in filtered_rows):,.2f} m")
    k2.metric("Pozos activos", f"{len({r.pozo for r in filtered})}")
    k3.metric("Semanas operativas", f"{len(filtered_rows)}")
    k4.metric("Reportes en rango", f"{len(filtered)}")

    # --- Weekly table -------------------------------------------------------
    st.subheader("📊 Avance semanal por fase")
    st.dataframe(
        build_weekly_df(filtered_rows, filtered_phases),
        width="stretch",
        hide_index=True,
    )

    # --- Charts -------------------------------------------------------------
    st.subheader("📈 Visualizaciones")
    tab1, tab2, tab3 = st.tabs(["Apilada por fase", "Gantt de pozos", "Acumulado"])
    with tab1:
        st.plotly_chart(
            stacked_bar_chart(filtered_rows, filtered_phases), width="stretch")
    with tab2:
        st.plotly_chart(gantt_chart(filtered), width="stretch")
    with tab3:
        st.plotly_chart(
            cumulative_chart(filtered_rows, filtered_phases), width="stretch")

    # --- Per-well summary ---------------------------------------------------
    well_summaries = summarize_wells(filtered)

    st.subheader("📋 Resumen por Pozo")
    st.caption(
        "Una fila por pozo, ordenada por **metros totales** descendente. "
        "Los markers `*` y `**` reflejan inicio/fin de perforación dentro "
        "del rango filtrado."
    )
    st.dataframe(
        build_well_summary_df(well_summaries),
        width="stretch",
        hide_index=True,
    )

    # --- Wells × Phases pivot -----------------------------------------------
    if well_summaries and filtered_phases:
        st.subheader("🔀 Pozo × Fase")
        st.caption(
            "Matriz de metros perforados por **(pozo, fase)**. Pozos con "
            "reubicación entre fases (ej. ZD-2833) aparecen en ambas columnas "
            "con los metros reales perforados en cada una. Celdas vacías = "
            "el pozo nunca operó en esa fase. La fila TOTAL coincide con la "
            "Σ Perforado del dataset (vista cruda), no con el total de la "
            "tabla semanal (que usa avance neto)."
        )
        pivot_df = build_wells_phase_pivot(
            well_summaries, filtered, filtered_phases
        )
        st.dataframe(
            pivot_df, width="stretch", hide_index=True
        )

    # --- Per-well analysis charts -------------------------------------------
    if well_summaries:
        st.subheader("📊 Análisis por Pozo")
        pa1, pa2, pa3 = st.tabs(["Top pozos", "Eficiencia", "Heatmap actividad"])
        with pa1:
            st.plotly_chart(top_wells_chart(well_summaries), width="stretch")
        with pa2:
            st.plotly_chart(efficiency_scatter(well_summaries), width="stretch")
        with pa3:
            st.plotly_chart(
                activity_heatmap(well_summaries, filtered), width="stretch")

        # --- Drilldown: detail for a single well ----------------------------
        st.subheader("🔍 Detalle por Pozo")
        well_options = [w.pozo for w in well_summaries]
        selected = st.selectbox(
            "Elegí un pozo para ver su evolución semanal:",
            options=well_options,
            index=0,
        )
        if selected:
            detail_df, detail_fig = well_detail_view(selected, filtered)
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Tabla semanal**")
                st.dataframe(
                    detail_df, width="stretch", hide_index=True
                )
            with c2:
                st.markdown("**Acumulado por fase**")
                st.plotly_chart(detail_fig, width="stretch")

    # --- Export -------------------------------------------------------------
    st.subheader("💾 Exportar")
    e1, e2 = st.columns(2)
    with e1:
        st.download_button(
            "⬇️  Descargar CSV",
            rows_to_csv(filtered_rows, filtered_phases),
            file_name="avance_semanal.csv",
            mime="text/csv",
            width="stretch",
        )
    with e2:
        st.download_button(
            "⬇️  Descargar Markdown",
            build_markdown(filtered_rows, data["warnings"], len(reports_all),
                           phases=filtered_phases),
            file_name="reporte.md",
            mime="text/markdown",
            width="stretch",
        )

    # --- Validations --------------------------------------------------------
    all_warns = data["warnings"] + data["issues"]
    test_issues = self_test(reports_all, rows_all)
    with st.expander(
        f"⚠️ Validaciones ({len(all_warns)} nota(s) · {len(test_issues)} self-test)"
    ):
        if not all_warns and not test_issues:
            st.success("Sin advertencias. Self-test pasó.")
        if all_warns:
            st.markdown("**Advertencias del parser / agregador:**")
            for w in all_warns:
                st.warning(w)
        if test_issues:
            st.markdown("**Self-test:**")
            for t in test_issues:
                st.error(t)
        elif not all_warns:
            st.success(
                "Self-test: Σ(Fases) = Total Semanal ✓ — sin pozos solapados."
            )

    # --- Footer -------------------------------------------------------------
    st.caption(
        f"Parser: {len(reports_all)} reportes extraídos · "
        f"{len(data['failures'])} mensajes no reconocidos · "
        f"{len({r.pozo for r in reports_all})} pozos únicos"
    )


if __name__ == "__main__":
    main()
