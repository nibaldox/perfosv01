# Perforación Diamantina — UI

UI web (Streamlit) sobre el parser `script.py`. Procesa un chat de WhatsApp con
reportes diarios de perforación diamantina y entrega el avance semanal por
fase, con tabla, gráficos, validaciones y exportación.

## Estructura

| Archivo             | Rol                                                     |
|---------------------|---------------------------------------------------------|
| `app.py`            | UI Streamlit (entrypoint)                               |
| `script.py`         | Parser y agregador (sin dependencias externas)          |
| `requirements.txt`  | Dependencias de la UI                                   |
| `_chat.txt`         | Ejemplo de entrada (chat real del proyecto)             |

## Ejecutar localmente

```bash
pip install -r requirements.txt
streamlit run app.py
```

Abre en `http://localhost:8501`. La carga del chat es **siempre manual** desde
el file uploader en la barra lateral — la app no lee archivos del disco
automáticamente.

## Deploy en Streamlit Community Cloud (gratis)

1. Suba el repositorio a GitHub (público o privado con la app autorizada).
2. Entrá a https://share.streamlit.io y conectá el repo.
3. **Main file**: `app.py`.
4. Deploy. Te da una URL fija tipo `tu-equipo.streamlit.app`.

> La app **duerme si no se usa por 7 días** en el plan free. Para mantenerla
> siempre activa: plan Teams (US$ /mes) o un deploy on-prem (Docker en un
> servidor interno).

## Tests del parser

```bash
python3 script.py --tests    # valida sin levantar la UI
```

## Features de la UI

- **Carga flexible**: arrastrar un `.txt` suelto o el `.zip` que entrega
  WhatsApp Web al exportar un chat (con o sin multimedia). La app extrae
  automáticamente el `.txt` del ZIP y reporta cuántos archivos multimedia
  se ignoraron. Si el ZIP tiene varios `.txt`, se queda con el más grande
  (suele ser el chat).
- **Filtros**: por fase, por pozo, por rango de fechas (con re-agregación).
- **KPIs**: metros totales, pozos activos, semanas operativas, reportes.
- **Tabla semanal**: avance por fase con `–` en celdas vacías.
- **Gráficos**:
  - Barras **apiladas** por fase (semanal).
  - **Gantt** de pozos coloreado por fase.
  - **Acumulado** por fase (línea).
- **Export**: CSV y Markdown descargables (mismo formato que el script CLI).
- **Validaciones**: advertencias de reasignación de fase, gaps de profundidad,
  inconsistencias en `Avance`, self-test (Σ(Fases) = Total, sin solapamiento).

## Privacidad

En Community Cloud, los datos del chat **salen a internet** y pasan por
servidores de Streamlit. Si los reportes contienen información sensible
(nombres de pozos, geología, metrajes), considere un deploy on-prem o use la
app solo localmente.
