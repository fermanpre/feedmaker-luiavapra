# Feedmaker

Genera feeds RSS de webs que no los tienen, para leerlas en Feedly. Uso personal.

## Cómo funciona

1. Cada web se añade en `feeds.json` (`id`, `nombre`, `url`).
2. Cada día (y al cambiar `feeds.json`) un workflow de GitHub Actions ejecuta `generar.py`:
   - Si la web ya tiene feed propio, no genera nada y lo anota en `docs/estado.json`.
   - Si no, detecta el listado de artículos de la página.
   - Con la clave de API configurada, Haiku 5.5 elige el listado correcto y marca las entradas
     que no son artículos (menús, secciones, promociones). Una entrada con fecha nunca se descarta.
3. Los feeds quedan en `docs/feeds/<id>.xml` y se publican con GitHub Pages en
   `https://<usuario>.github.io/<repositorio>/feeds/<id>.xml`.

## Puesta en marcha

1. Repositorio en GitHub con estos ficheros.
2. Settings → Actions → General → Workflow permissions: **Read and write permissions**.
3. (Opcional, para usar la IA) Settings → Secrets and variables → Actions → New repository secret:
   `ANTHROPIC_API_KEY`. Sin ella funciona igual, pero sin filtrar y sin elegir el listado con IA.
4. Settings → Pages → Deploy from a branch → rama `main`, carpeta `/docs`.
5. Lanzar el workflow a mano desde la pestaña Actions y mirar `docs/estado.json`.

## Opciones por web en `feeds.json`

| Campo | Efecto |
|---|---|
| `user_agent` | `"auto"` (por defecto): se identifica como `feedmaker` y, solo si la web responde 403 o 406, repite la petición presentándose como un navegador. `"honesto"`: nunca se presenta como navegador. `"navegador"`: siempre lo hace. `estado.json` anota cuál se usó en cada web. |
| `forzar_tarjetas: true` | Genera el feed aunque la web ya tenga uno propio. |

`robots.txt` se respeta siempre. Si una web lo prohíbe, no se descarga.

## Límites conocidos

- No funciona con páginas que construyen el listado con JavaScript.
- Algunas webs bloquean las IP de GitHub o dan 403 aunque se presente como navegador.
- Un cambio de diseño puede romper la detección: el feed anterior se mantiene y `estado.json` lo anota.
- La detección no es perfecta; a veces cuela alguna entrada de relleno o descarta una noticia.

## Probarlo en local

    python generar.py feeds.json

Feed no oficial, solo para uso personal. Los contenidos son de sus autores.
