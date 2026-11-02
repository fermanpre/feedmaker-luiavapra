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

## Página web: pegar una URL y obtener el enlace de Feedly

La página `https://<usuario>.github.io/<repositorio>/` tiene un campo para la URL.
Una página de GitHub Pages no puede escribir en el repositorio, así que el alta pasa por una incidencia:

1. Pegas la URL y pulsas «Obtener enlace». Se abre GitHub con una incidencia ya rellenada (`alta: <url>`).
2. Pulsas «Submit new issue».
3. El workflow `alta.yml` añade la web a `feeds.json`, genera el feed, responde en la incidencia y la cierra.
4. La página, que consulta `estado.json` cada 10 segundos, muestra el enlace para Feedly.

Solo el propietario del repositorio puede dar de alta webs: cualquiera puede abrir incidencias en un
repositorio público, y cada alta consume llamadas a la API de pago.

Dos alternativas sin incidencia, con más comodidad:

- **Token de GitHub** (solo para ti): en «Conexión con GitHub» de la página, pegas un token con permiso de
  Actions y el botón lanza el workflow directamente.
- **Servicio intermedio con contraseña** (para compartirlo): un Cloudflare Worker guarda el token y la página
  solo envía una contraseña. Ver `worker/README.md`.

La casilla «Incluir el texto completo de los artículos» da de alta la web con `texto_completo: true`. Solo se aplica
a webs nuevas: si la web ya existe, se cambia a mano en `feeds.json`. Con el servicio intermedio hay que desplegar
de nuevo el Worker (`npx wrangler deploy`) para que reenvíe la opción.

La página detecta una web ya dada de alta aunque la escribas con o sin `www`, con o sin barra final o con
parámetros de seguimiento (`utm_…`).

## Opciones por web en `feeds.json`

| Campo | Efecto |
|---|---|
| `user_agent` | `"auto"` (por defecto): se identifica como `feedmaker` y, solo si la web responde 403 o 406, repite la petición presentándose como un navegador. `"honesto"`: nunca se presenta como navegador. `"navegador"`: siempre lo hace. `estado.json` anota cuál se usó en cada web. |
| `forzar_tarjetas: true` | Genera el feed aunque la web ya tenga uno propio. |
| `texto_completo: true` | Descarga cada artículo y mete su texto en el feed (`content:encoded`), no solo el extracto. Sin IA: es extracción por reglas. Se descargan como máximo 15 artículos nuevos por ejecución (`FEEDMAKER_MAX_ARTICULOS`); lo ya descargado se guarda en `datos/articulos.json`. |
| `conservar_dias` | Días que se conserva en el feed una entrada que la web ya no enseña (por defecto 180). Cuenta desde la última vez que se vio, no desde su fecha: lo que sigue listado no caduca. `0` = solo lo que enseña hoy la web. |
| `leer_feed_propio: true` | Si la web tiene un feed que corresponde a la página, en vez de responder «ya tiene feed» se leen sus entradas, que traen fechas reales, y se guardan en el historial. Sirve para alimentar un combinado. |
| `etiqueta` | Nombre corto que antepone a los títulos cuando la web forma parte de un combinado: «[ABC] …». Por defecto, el nombre de la web. |
| `oculto: true` | La página no enseña la web en «Mis feeds». Para webs que solo son ingredientes de un combinado. |
| `modo_enlaces: true` | En vez de buscar un bloque de tarjetas, recoge todos los enlaces de la página que parecen artículos (de la misma web y con una dirección larga de 5 palabras o más). Saca la fecha de la propia dirección si lleva marca de tiempo. Para páginas con el listado repartido en bloques distintos, como la sección de ABC. Sin IA. |
| `filtro_en_combinado` | Lista de palabras que sustituye, solo para esta web, al filtro del combinado. |

**Aviso:** con `texto_completo`, el repositorio y los feeds, que son públicos, contienen copias del texto de
webs ajenas. Úsalo solo en las pocas webs que realmente lees.

`robots.txt` se respeta siempre. Si una web lo prohíbe, no se descarga.

## Feeds combinados

Una entrada de `feeds.json` con `combina` junta en un solo feed las entradas del historial de varias webs:

    {"id": "caja87", "nombre": "Caja87", "combina": ["web-a", "web-b"], "filtro": ["caja 87", "caja87"]}

- `combina`: los `id` de las webs, que se procesan como cualquier otra (se recomienda marcarlas `oculto`).
- `filtro` (opcional): solo pasan las entradas cuyo título o extracto contiene alguna de esas palabras o frases, como
  palabra completa (`caja` no coincide con `Cajasol`). Sin filtro pasan todas.
- Cada título lleva delante la web de origen, y se quitan los enlaces repetidos entre webs.
- El combinado se alimenta del historial de cada web, así que gana la misma profundidad (`conservar_dias`).
- Una web que solo ofrece feed propio no aporta entradas al combinado, salvo que lleve `leer_feed_propio: true`.

## Historial de entradas

Las webs solo enseñan sus últimas entradas, y las antiguas desaparecen de la página. Para que no desaparezcan del feed,
`datos/historial.json` guarda por web el título, el enlace, la fecha y el extracto de cada entrada vista. El feed se construye
con todas las del historial (máximo 300, las más recientes primero). Una entrada se borra cuando lleva más de
`conservar_dias` días sin aparecer en la web. El historial empieza el día en que se activa: no recupera entradas anteriores.
Con `texto_completo`, el texto de esas entradas sale de `datos/articulos.json`, que caduca con el historial.

## Feed genérico enlazado desde una página específica

Si la página enlaza un feed, se usa ese salvo que sea más general que la página (el feed de toda la sección
Deportes enlazado desde la página de un solo equipo). Se considera general si la ruta de la página no aparece
en la URL del feed y menos del 40 % de las 10 entradas más recientes del feed están enlazadas en la página.
En ese caso se genera el feed de la página, y si no sale ninguno se conserva el feed propio.

## Límites conocidos

- No funciona con páginas que construyen el listado con JavaScript.
- Algunas webs bloquean las IP de GitHub o dan 403 aunque se presente como navegador.
- Un cambio de diseño puede romper la detección: el feed anterior se mantiene y `estado.json` lo anota.
- La detección no es perfecta; a veces cuela alguna entrada de relleno o descarta una noticia.

## Probarlo en local

    python generar.py feeds.json

Feed no oficial, solo para uso personal. Los contenidos son de sus autores.
