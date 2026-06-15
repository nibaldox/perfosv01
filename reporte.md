# Resumen Semanal de Perforación Diamantina

_Reportes diarios procesados: **158**_

| Mes - Semana | FASE 14 (m) | FASE 9 (m) | FASE 8S (m) | FASE 12 (m) | FASE 13 (m) | Pozo(s) en ejecución | Total Semanal (m) |
|---|---|---|---|---|---|---|---|
| Marzo - S11 | 128.85 | – | – | – | – | ZD-2817<br>ZD-2818** | 128.85 |
| Marzo - S12 | 142.55 | 120.00 | – | – | – | ZD-2818**<br>ZD-2820 | 262.55 |
| Marzo - S13 | – | 292.90 | – | – | – | ZD-2823**<br>ZD-2824**<br>ZD-2825** | 292.90 |
| Marzo - S14 | 260.20 | – | – | – | – | ZD-2825** | 260.20 |
| Abril - S15 | – | – | 100.00 | 222.35 | – | ZD-2829**<br>ZD-2830<br>ZD-2832 | 322.35 |
| Abril - S16 | – | – | – | 155.00 | 210.70 | ZD-2832**<br>ZD-2833 | 365.70 |
| Abril - S17 | – | – | – | 69.30 | 244.85 | ZD-2833**<br>ZD-2835 | 314.15 |
| Abril - S18 | – | – | – | 70.85 | 127.75 | ZD-2835**<br>ZD-2837** | 198.60 |
| Mayo - S19 | – | – | – | 326.65 | – | ZD-2837**<br>ZD-2839** | 326.65 |
| Mayo - S20 | – | – | – | 255.15 | – | ZD-2839**<br>ZD-2842<br>ZD-2843** | 255.15 |
| Mayo - S21 | – | – | – | – | 151.55 | ZD-2843 | 151.55 |
| Junio - S23 | – | – | – | 228.55 | – | ZD-2849**<br>ZD-2851** | 228.55 |
| Junio - S24 | – | – | – | 158.90 | – | ZD-2851 | 158.90 |
| **TOTAL** | **531.60** | **412.90** | **100.00** | **1486.75** | **734.85** |  | **3266.10** |

## Totales por mes

| Mes | FASE 14 | FASE 9 | FASE 8S | FASE 12 | FASE 13 | Total (m) |
|---|---|---|---|---|---|---|
| Marzo | 531.60 | 412.90 | – | – | – | 944.50 |
| Abril | – | – | 100.00 | 517.50 | 583.30 | 1200.80 |
| Mayo | – | – | – | 581.80 | 151.55 | 733.35 |
| Junio | – | – | – | 387.45 | – | 387.45 |

## 📌 Notas técnicas

- **Fases normalizadas** a `FASE_NN`. Variantes como `Fase 14`, `FASE 8 sur`, `FASE 8S`, `FASE 8 S` y `FASE 09` se canonicalizan automáticamente.
- **Reubicaciones de plataforma** (p. ej. `ZD-2833`, que alterna entre Fase 12 y Fase 13 entre turnos): se elige la fase con más registros de avance no nulo; en empate, la fase donde el pozo alcanza mayor profundidad final. Todos los metros semanales del pozo se asignan a esa fase canónica.
- **Avance semanal por fase** = `max(Fondo) − min(Inicio)` considerando todos los turnos del mismo pozo dentro de la semana (lunes a domingo, semana ISO). Si faltan `Desde`/`Hasta`, se usa `Σ Perforado` como respaldo.
- **Decimales**: el parser acepta coma o punto como separador (`115,00 mts` ≡ `115.00 mts`); se ignoran unidades (`m`, `mts`, `mt`, `°`, etc.).
- **Marcadores en `Pozo(s) en ejecución`**: `*` indica pozo que inició perforación en la semana (turno de instalación, `inicio = fondo = 0`); `**` indica pozo finalizado/desarmado/trasladado en la semana. Un mismo pozo puede llevar ambos en la misma semana.
- **Celdas vacías** se muestran como `–`.
- **Tolerancia de validación**: ±0.1 m entre avance neto y suma de perforado. Las discrepancias se listan en la sección de advertencias.

## ⚠️ Advertencias de validación
- Semana 2026-03-30: ZD-2825 en FASE 14 — avance neto 260.20 m vs Σ Perforado 409.50 m (Δ -149.30 m).
- Semana 2026-04-06: ZD-2832 en FASE 12 — avance neto 87.35 m vs Σ Perforado 52.85 m (Δ +34.50 m).
- Semana 2026-04-13: ZD-2833 aparece en fases ['FASE 12', 'FASE 13'] — asignado a FASE 13 (fase dominante).
- Semana 2026-04-20: ZD-2833 aparece en fases ['FASE 12', 'FASE 13'] — asignado a FASE 12 (fase dominante).
- Semana 2026-04-27: ZD-2835 en FASE 13 — avance neto 127.75 m vs Σ Perforado 127.23 m (Δ +0.52 m).
- Semana 2026-05-18: ZD-2843 en FASE 13 — avance neto 151.55 m vs Σ Perforado 99.25 m (Δ +52.30 m).
- ZD-2825 2026-03-31 Noche: Fondo−Inicio=3.00 m vs Avance=3.30 m (Δ -0.30 m)
- ZD-2825 2026-04-02 Noche: Fondo−Inicio=18.20 m vs Avance=167.20 m (Δ -149.00 m)
- ZD-2833 2026-04-17 Día: Fondo−Inicio=45.20 m vs Avance=46.20 m (Δ -1.00 m)
- ZD-2835 2026-04-27 Día: Fondo−Inicio=0.60 m vs Avance=0.00 m (Δ +0.60 m)
