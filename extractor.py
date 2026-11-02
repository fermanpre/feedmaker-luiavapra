"""Extractor general de Feedmaker: dada una URL, intenta obtener un listado de entradas.

Nivel 1: descubrir un feed RSS/Atom que la web ya tenga.
Nivel 2: detectar un listado de «tarjetas» repetidas en el HTML (enlace + título + fecha + extracto).

Solo biblioteca estándar. Se identifica con un User-Agent honesto y respeta robots.txt
(RFC 9309): 2xx se interpreta, 4xx = sin restricciones, 5xx o fallo de red = no se rastrea.
"""

import html
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from xml.sax.saxutils import escape

AGENTE_USUARIO = "feedmaker/0.1 (+https://github.com/fermanpre/feedmaker)"
# Uso personal: FEEDMAKER_UA cambia la cabecera al descargar; robots.txt se sigue evaluando con AGENTE_USUARIO.
CABECERA_USER_AGENT = os.environ.get("FEEDMAKER_UA") or AGENTE_USUARIO
TIEMPO_MAXIMO_PETICION = 20
TAMANO_MAXIMO_RESPUESTA = 2_000_000
PAUSA_ENTRE_PETICIONES = 1.0

MINIMO_ELEMENTOS_GRUPO = 5
MINIMO_LONGITUD_TITULO = 10
MINIMO_LONGITUD_TITULO_MEDIA = 15
MINIMO_LONGITUD_TITULO_SIN_FECHAS = 25
MINIMO_SLUG_MEDIO = 20
PATRON_ZONA_NO_EDITORIAL = re.compile(
    r"(^|[\s_-])(menu|nav|navbar|footer|breadcrumb|cookie|cookies|consent|banner|promo|"
    r"sponsor|sponsored|advert|ad|ads|adslot)($|[\s_-])",
    re.IGNORECASE,
)
MAXIMO_ENTRADAS = 50
RUTAS_TIPICAS_DE_FEED = ["feed", "feed/", "rss", "rss.xml", "atom.xml", "index.xml", "feed.xml"]

ETIQUETAS_VACIAS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
ETIQUETAS_IGNORADAS = {"script", "style", "noscript", "svg", "template", "head"}
ETIQUETAS_DE_ESTRUCTURA_NO_EDITORIAL = {"nav", "header", "footer", "aside"}
ETIQUETAS_DE_TITULAR = {"h1", "h2", "h3", "h4", "h5", "h6"}

MESES = {
    "ene": 1, "enero": 1, "jan": 1, "january": 1,
    "feb": 2, "febrero": 2, "february": 2,
    "mar": 3, "marzo": 3, "march": 3,
    "abr": 4, "abril": 4, "apr": 4, "april": 4,
    "may": 5, "mayo": 5,
    "jun": 6, "junio": 6, "june": 6,
    "jul": 7, "julio": 7, "july": 7,
    "ago": 8, "agosto": 8, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "set": 9, "septiembre": 9, "setiembre": 9, "september": 9,
    "oct": 10, "octubre": 10, "october": 10,
    "nov": 11, "noviembre": 11, "november": 11,
    "dic": 12, "diciembre": 12, "dec": 12, "december": 12,
}
NOMBRES_DE_MES = "|".join(sorted(MESES, key=len, reverse=True))
PATRON_FECHA_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")  # admite «2026-10-16T23:06…»
PATRON_FECHA_NUMERICA = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")
PATRON_DIA_MES_ANIO = re.compile(
    rf"\b(\d{{1,2}})\s*(?:de\s+)?({NOMBRES_DE_MES})\.?,?\s*(?:de\s+)?(\d{{4}})\b", re.IGNORECASE
)
PATRON_MES_DIA_ANIO = re.compile(
    rf"\b({NOMBRES_DE_MES})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE
)


# --------------------------------------------------------------------------- HTTP

class RespuestaHttp:
    def __init__(self, estado, url_final, tipo_contenido, texto, error=""):
        self.estado = estado
        self.url_final = url_final
        self.tipo_contenido = tipo_contenido
        self.texto = texto
        self.error = error


_ultima_peticion_por_host = {}
_robots_por_host = {}


def _esperar_turno(host):
    ultima = _ultima_peticion_por_host.get(host, 0.0)
    espera = PAUSA_ENTRE_PETICIONES - (time.monotonic() - ultima)
    if espera > 0:
        time.sleep(espera)
    _ultima_peticion_por_host[host] = time.monotonic()


def _decodificar(contenido, cabecera_tipo):
    juego = None
    coincidencia = re.search(r"charset=([\w-]+)", cabecera_tipo or "", re.IGNORECASE)
    if coincidencia:
        juego = coincidencia.group(1)
    if not juego:
        coincidencia = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", contenido[:4000], re.IGNORECASE)
        if coincidencia:
            juego = coincidencia.group(1).decode("ascii", "ignore")
    for candidato in (juego, "utf-8"):
        if not candidato:
            continue
        try:
            return contenido.decode(candidato, errors="replace")
        except LookupError:
            continue
    return contenido.decode("utf-8", errors="replace")


def peticion(url):
    """GET con User-Agent honesto, pausa por host y límite de tamaño. No mira robots.txt."""
    host = urllib.parse.urlsplit(url).netloc
    _esperar_turno(host)
    solicitud = urllib.request.Request(url, headers={
        "User-Agent": CABECERA_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,application/rss+xml,*/*;q=0.5",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(solicitud, timeout=TIEMPO_MAXIMO_PETICION) as respuesta:
            tipo = respuesta.headers.get("Content-Type", "")
            contenido = respuesta.read(TAMANO_MAXIMO_RESPUESTA)
            return RespuestaHttp(respuesta.status, respuesta.geturl(), tipo, _decodificar(contenido, tipo))
    except urllib.error.HTTPError as error:
        return RespuestaHttp(error.code, url, "", "", error=f"HTTP {error.code}")
    except Exception as error:
        return RespuestaHttp(0, url, "", "", error=f"{type(error).__name__}: {error}")


def _cargar_robots(esquema, host):
    clave = (esquema, host)
    if clave in _robots_por_host:
        return _robots_por_host[clave]
    respuesta = peticion(f"{esquema}://{host}/robots.txt")
    analizador = RobotFileParser()
    if 200 <= respuesta.estado < 300:
        analizador.parse(respuesta.texto.splitlines())
    elif 400 <= respuesta.estado < 500:
        analizador.parse([])  # sin robots.txt: todo permitido (RFC 9309, sección 2.3.1.3)
    else:
        analizador.parse(["User-agent: *", "Disallow: /"])  # 5xx o red caída: no rastrear
    _robots_por_host[clave] = analizador
    return analizador


def permitido_por_robots(url):
    partes = urllib.parse.urlsplit(url)
    analizador = _cargar_robots(partes.scheme, partes.netloc)
    return analizador.can_fetch(AGENTE_USUARIO, url)


def descargar_respetuosamente(url):
    """Devuelve (RespuestaHttp, motivo_de_bloqueo). Si robots.txt lo prohíbe, no se descarga."""
    if not permitido_por_robots(url):
        return RespuestaHttp(0, url, "", "", error="robots.txt"), "robots.txt lo prohíbe"
    return peticion(url), ""


# --------------------------------------------------------------------------- DOM mínimo

class Nodo:
    def __init__(self, etiqueta, atributos=None, padre=None, contenido=""):
        self.etiqueta = etiqueta
        self.atributos = atributos or {}
        self.padre = padre
        self.hijos = []
        self.contenido = contenido
        self.clases = tuple(sorted((self.atributos.get("class") or "").split()))

    def es_texto(self):
        return self.etiqueta == "#texto"


class ConstructorDeArbol(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raiz = Nodo("#raiz")
        self.pila = [self.raiz]
        self.profundidad_ignorada = 0

    def handle_starttag(self, etiqueta, atributos):
        if self.profundidad_ignorada:
            if etiqueta in ETIQUETAS_IGNORADAS:
                self.profundidad_ignorada += 1
            return
        if etiqueta in ETIQUETAS_IGNORADAS:
            self.profundidad_ignorada = 1
            return
        if etiqueta == "li":
            self._cerrar_li_abierto()
        nodo = Nodo(etiqueta, dict(atributos), self.pila[-1])
        self.pila[-1].hijos.append(nodo)
        if etiqueta not in ETIQUETAS_VACIAS:
            self.pila.append(nodo)

    def handle_startendtag(self, etiqueta, atributos):
        if self.profundidad_ignorada or etiqueta in ETIQUETAS_IGNORADAS:
            return
        nodo = Nodo(etiqueta, dict(atributos), self.pila[-1])
        self.pila[-1].hijos.append(nodo)

    def handle_endtag(self, etiqueta):
        if self.profundidad_ignorada:
            if etiqueta in ETIQUETAS_IGNORADAS:
                self.profundidad_ignorada -= 1
            return
        for posicion in range(len(self.pila) - 1, 0, -1):
            if self.pila[posicion].etiqueta == etiqueta:
                del self.pila[posicion:]
                return

    def handle_data(self, texto):
        if self.profundidad_ignorada or not texto.strip():
            return
        self.pila[-1].hijos.append(Nodo("#texto", padre=self.pila[-1], contenido=texto))

    def _cerrar_li_abierto(self):
        """Un <li> sin cerrar no debe anidar al siguiente <li>."""
        for posicion in range(len(self.pila) - 1, 0, -1):
            etiqueta = self.pila[posicion].etiqueta
            if etiqueta in ("ul", "ol"):
                return
            if etiqueta == "li":
                del self.pila[posicion:]
                return


def construir_arbol(html):
    constructor = ConstructorDeArbol()
    constructor.feed(html)
    constructor.close()
    return constructor.raiz


def descendientes(nodo):
    for hijo in nodo.hijos:
        yield hijo
        yield from descendientes(hijo)


def texto_de(nodo):
    if nodo.es_texto():
        return nodo.contenido
    fragmentos = [texto_de(hijo) for hijo in nodo.hijos]
    return " ".join(" ".join(fragmentos).split())


def antepasados(nodo):
    actual = nodo.padre
    while actual is not None:
        yield actual
        actual = actual.padre


def descripcion_corta(nodo):
    clases = ".".join(nodo.clases[:2])
    return f"{nodo.etiqueta}.{clases}" if clases else nodo.etiqueta


# --------------------------------------------------------------------------- fechas

def interpretar_fecha(texto):
    """Busca una fecha en un texto (ISO, dd/mm/aaaa, «23 Oct, 2026», «October 23, 2026»)."""
    if not texto:
        return None
    try:
        coincidencia = PATRON_FECHA_ISO.search(texto)
        if coincidencia:
            anio, mes, dia = (int(valor) for valor in coincidencia.groups())
            return datetime(anio, mes, dia, 12, 0, tzinfo=timezone.utc)
        coincidencia = PATRON_DIA_MES_ANIO.search(texto)
        if coincidencia:
            dia, nombre_mes, anio = coincidencia.groups()
            return datetime(int(anio), MESES[nombre_mes.lower()], int(dia), 12, 0, tzinfo=timezone.utc)
        coincidencia = PATRON_MES_DIA_ANIO.search(texto)
        if coincidencia:
            nombre_mes, dia, anio = coincidencia.groups()
            return datetime(int(anio), MESES[nombre_mes.lower()], int(dia), 12, 0, tzinfo=timezone.utc)
        coincidencia = PATRON_FECHA_NUMERICA.search(texto)
        if coincidencia:
            dia, mes, anio = (int(valor) for valor in coincidencia.groups())
            return datetime(anio, mes, dia, 12, 0, tzinfo=timezone.utc)
    except ValueError:
        return None
    return None


# --------------------------------------------------------------------------- nivel 2

def _es_enlace_util(nodo):
    if nodo.etiqueta != "a":
        return False
    destino = (nodo.atributos.get("href") or "").strip()
    return bool(destino) and not destino.startswith(("#", "javascript:", "mailto:", "tel:"))


def _es_solo_una_fecha(texto):
    return len(texto) <= 30 and interpretar_fecha(texto) is not None


def _buscar_nodo_de_titulo(elementos):
    """Primer titular (h1-h6) y, si no hay, primer strong/b con texto de título; nunca una fecha."""
    for etiquetas in (ETIQUETAS_DE_TITULAR, {"strong", "b"}):
        for nodo in elementos:
            if nodo.etiqueta in etiquetas:
                candidato = texto_de(nodo)
                if len(candidato) >= MINIMO_LONGITUD_TITULO and not _es_solo_una_fecha(candidato):
                    return nodo
    return None


def extraer_entrada(nodo_item, url_base):
    """Devuelve dict(titulo, enlace, fecha, extracto) o None si el elemento no parece una entrada."""
    elementos = [nodo_item] + list(descendientes(nodo_item))
    enlaces = [nodo for nodo in elementos if _es_enlace_util(nodo)]
    if not enlaces:
        return None

    nodo_titulo = _buscar_nodo_de_titulo(elementos)
    if nodo_titulo is not None:
        titulo = texto_de(nodo_titulo)
        enlace_titulo = next((a for a in [nodo_titulo] + list(descendientes(nodo_titulo)) if _es_enlace_util(a)), None)
        if enlace_titulo is None:
            for antepasado in antepasados(nodo_titulo):
                if _es_enlace_util(antepasado):
                    enlace_titulo = antepasado
                    break
                if antepasado is nodo_item.padre:
                    break
    else:
        titulo = ""
        enlace_titulo = None
    if enlace_titulo is None:
        enlace_titulo = max(enlaces, key=lambda a: len(texto_de(a)))
    if not titulo:
        titulo = texto_de(enlace_titulo)[:160]
    if len(titulo) < MINIMO_LONGITUD_TITULO or len(titulo.split()) < 2:
        return None

    enlace = urllib.parse.urljoin(url_base, enlace_titulo.atributos["href"].strip())
    if not enlace.startswith(("http://", "https://")):
        return None

    fecha = None
    for nodo in elementos:
        if nodo.etiqueta == "time":
            fecha = interpretar_fecha(nodo.atributos.get("datetime", "")) or interpretar_fecha(texto_de(nodo))
            if fecha:
                break
    if fecha is None:
        fecha = interpretar_fecha(texto_de(nodo_item))

    extracto = ""
    for nodo in elementos:
        if nodo.etiqueta == "p":
            candidato = texto_de(nodo)
            if len(candidato) >= 40 and candidato != titulo:
                extracto = candidato
                break
    return {"titulo": titulo, "enlace": enlace, "fecha": fecha, "extracto": extracto}


def _host_sin_www(url):
    host = urllib.parse.urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _grupos_de_hermanos(contenedor):
    """Agrupa los hijos-elemento por firma de clases y, como alternativa, solo por etiqueta."""
    hijos_elemento = [h for h in contenedor.hijos if not h.es_texto()]
    por_clases = {}
    por_etiqueta = {}
    for hijo in hijos_elemento:
        por_clases.setdefault((hijo.etiqueta, hijo.clases), []).append(hijo)
        por_etiqueta.setdefault(hijo.etiqueta, []).append(hijo)
    for grupo in list(por_clases.values()) + list(por_etiqueta.values()):
        if len(grupo) >= MINIMO_ELEMENTOS_GRUPO:
            yield grupo


def _puntuar_grupo(grupo, url_base, aplicar_filtros=True):
    entradas = []
    for nodo_item in grupo:
        entrada = extraer_entrada(nodo_item, url_base)
        if entrada:
            entradas.append(entrada)
    if len(entradas) < MINIMO_ELEMENTOS_GRUPO or len(entradas) < 0.6 * len(grupo):
        return 0.0, entradas
    enlaces_unicos = {entrada["enlace"] for entrada in entradas}
    if len(enlaces_unicos) < 0.8 * len(entradas):
        return 0.0, entradas
    longitud_media = sum(len(e["titulo"]) for e in entradas) / len(entradas)
    con_fecha = sum(1 for e in entradas if e["fecha"]) / len(entradas)
    con_extracto = sum(1 for e in entradas if e["extracto"]) / len(entradas)
    slug_medio = sum(_longitud_del_slug(e["enlace"]) for e in entradas) / len(entradas)
    con_numeros = sum(1 for e in entradas if _tiene_numeros_en_la_ruta(e["enlace"])) / len(entradas)

    # Filtros anti-menú: los menús, categorías y anuncios tienen títulos cortos, enlaces con
    # rutas cortas y ninguna señal editorial (fecha, extracto, números en la URL).
    if longitud_media < MINIMO_LONGITUD_TITULO_MEDIA:
        return 0.0, entradas
    if aplicar_filtros:
        if longitud_media < MINIMO_LONGITUD_TITULO_SIN_FECHAS and not (con_fecha >= 0.7):
            return 0.0, entradas
        sin_senales_editoriales = (
            con_fecha < 0.3 and con_extracto < 0.3
            and slug_medio < MINIMO_SLUG_MEDIO and con_numeros < 0.5
        )
        if sin_senales_editoriales:
            return 0.0, entradas

    mismo_host = sum(1 for e in entradas if _host_sin_www(e["enlace"]) == _host_sin_www(url_base)) / len(entradas)
    penalizacion = 1.0
    if any(a.etiqueta in ETIQUETAS_DE_ESTRUCTURA_NO_EDITORIAL for a in antepasados(grupo[0])):
        penalizacion = 0.2
    if _parece_zona_no_editorial(grupo[0]):
        penalizacion = min(penalizacion, 0.2)
    puntuacion = len(entradas) * (1 + con_fecha + 0.5 * con_extracto) * (0.5 + 0.5 * mismo_host) * penalizacion
    return puntuacion, entradas


def _longitud_del_slug(enlace):
    ruta = urllib.parse.urlsplit(enlace).path.rstrip("/")
    ultimo_tramo = ruta.rsplit("/", 1)[-1]
    return len(re.sub(r"\.\w{2,5}$", "", ultimo_tramo))


def _tiene_numeros_en_la_ruta(enlace):
    return len(re.findall(r"\d", urllib.parse.urlsplit(enlace).path)) >= 4


def _parece_zona_no_editorial(nodo):
    """True si el nodo, o alguno de sus antepasados, lleva clases o id de menú, anuncio o pie."""
    for candidato in [nodo] + list(antepasados(nodo)):
        texto = " ".join(candidato.clases) + " " + (candidato.atributos.get("id") or "")
        if PATRON_ZONA_NO_EDITORIAL.search(texto):
            return True
    return False


def _calcular_confianza(entradas):
    con_fecha = sum(1 for e in entradas if e["fecha"]) / len(entradas)
    con_extracto = sum(1 for e in entradas if e["extracto"]) / len(entradas)
    slug_medio = sum(_longitud_del_slug(e["enlace"]) for e in entradas) / len(entradas)
    if con_fecha >= 0.7 and (con_extracto >= 0.3 or slug_medio >= MINIMO_SLUG_MEDIO):
        return "alta"
    if con_fecha >= 0.3 or slug_medio >= 25:
        return "media"
    return "baja"


def detectar_candidatos(html, url_base, maximo=5):
    """Nivel 3 (preparación): los mejores grupos de tarjetas SIN filtros anti-menú, para que un modelo elija.

    Devuelve una lista de dict(firma, confianza, puntuacion, entradas) ordenada de mayor a menor puntuación.
    """
    raiz = construir_arbol(html)
    candidatos = {}
    pendientes = [raiz]
    while pendientes:
        contenedor = pendientes.pop()
        pendientes.extend(h for h in contenedor.hijos if not h.es_texto())
        for grupo in _grupos_de_hermanos(contenedor):
            puntuacion, entradas = _puntuar_grupo(grupo, url_base, aplicar_filtros=False)
            if puntuacion <= 0:
                continue
            firma = f"{descripcion_corta(contenedor)} > {descripcion_corta(grupo[0])} (x{len(grupo)})"
            if firma not in candidatos or puntuacion > candidatos[firma]["puntuacion"]:
                candidatos[firma] = {
                    "firma": firma, "confianza": _calcular_confianza(entradas),
                    "puntuacion": round(puntuacion, 2), "entradas": entradas,
                }
    ordenados = sorted(candidatos.values(), key=lambda c: c["puntuacion"], reverse=True)
    return ordenados[:maximo]


def detectar_tarjetas(html, url_base):
    """Nivel 2. Devuelve dict(entradas, firma, puntuacion) o None."""
    raiz = construir_arbol(html)
    mejor = None
    pendientes = [raiz]
    while pendientes:
        contenedor = pendientes.pop()
        pendientes.extend(h for h in contenedor.hijos if not h.es_texto())
        for grupo in _grupos_de_hermanos(contenedor):
            puntuacion, entradas = _puntuar_grupo(grupo, url_base)
            if puntuacion > 0 and (mejor is None or puntuacion > mejor["puntuacion"]):
                mejor = {
                    "entradas": entradas,
                    "confianza": _calcular_confianza(entradas),
                    "puntuacion": round(puntuacion, 2),
                    "firma": f"{descripcion_corta(contenedor)} > {descripcion_corta(grupo[0])} (x{len(grupo)})",
                }
    return mejor


# --------------------------------------------------------------------------- nivel 1

class BuscadorDeEnlacesAlternativos(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.candidatos = []

    def handle_starttag(self, etiqueta, atributos):
        if etiqueta != "link":
            return
        atributos = dict(atributos)
        relacion = (atributos.get("rel") or "").lower()
        tipo = (atributos.get("type") or "").lower()
        if "alternate" in relacion and ("rss" in tipo or "atom" in tipo) and atributos.get("href"):
            self.candidatos.append(atributos["href"])


def _parece_feed(respuesta):
    if not (200 <= respuesta.estado < 300):
        return False
    inicio = respuesta.texto[:3000].lower()
    return "<rss" in inicio or "<feed" in inicio or "<rdf:rdf" in inicio


def contar_entradas_de_feed(texto):
    return len(re.findall(r"<item[\s>]|<entry[\s>]", texto, re.IGNORECASE))


def descubrir_feed(html, url_pagina):
    """Nivel 1. Devuelve (url_del_feed, numero_de_entradas) o (None, 0)."""
    buscador = BuscadorDeEnlacesAlternativos()
    buscador.feed(html[:200_000])
    candidatos = [urllib.parse.urljoin(url_pagina, enlace) for enlace in buscador.candidatos]
    if not candidatos:
        partes = urllib.parse.urlsplit(url_pagina)
        raiz = f"{partes.scheme}://{partes.netloc}/"
        base_pagina = url_pagina if url_pagina.endswith("/") else url_pagina + "/"
        for ruta in RUTAS_TIPICAS_DE_FEED:
            candidatos.append(urllib.parse.urljoin(raiz, ruta))
            if base_pagina != raiz:
                candidatos.append(urllib.parse.urljoin(base_pagina, ruta))
    vistos = set()
    for candidato in candidatos[:8]:
        if candidato in vistos or not permitido_por_robots(candidato):
            continue
        vistos.add(candidato)
        respuesta = peticion(candidato)
        if _parece_feed(respuesta):
            return respuesta.url_final, contar_entradas_de_feed(respuesta.texto)
    return None, 0


ENTRADAS_RECIENTES_A_COMPARAR = 10
UMBRAL_FEED_DE_LA_PAGINA = 0.4


def _enlace_comparable(url):
    """Forma de una URL para compararla: sin esquema, sin «www.», sin barra final ni fragmento."""
    partes = urllib.parse.urlsplit(url.strip())
    host = partes.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host + partes.path.rstrip("/")


def _enlaces_de_las_entradas_del_feed(texto_del_feed):
    """Enlaces de las entradas, en el orden del feed. Solo se miran los de dentro de cada <item> o <entry>:
    el enlace del canal (la portada de la web) no es una noticia."""
    bloques = re.findall(r"<(?:item|entry)[\s>].*?</(?:item|entry)>", texto_del_feed, re.IGNORECASE | re.DOTALL)
    enlaces = []
    for bloque in bloques:
        coincidencia = (re.search(r"<link>\s*(https?://[^<\s]+)\s*</link>", bloque)
                        or re.search(r"<link[^>]+href=[\"'](https?://[^\"']+)[\"']", bloque))
        if coincidencia:
            enlaces.append(_enlace_comparable(coincidencia.group(1)))
    return list(dict.fromkeys(enlaces))


def proporcion_del_feed_en_la_pagina(html, url_pagina, url_del_feed):
    """Qué parte de las entradas más recientes del feed está enlazada en la página, de 0 a 1; None si no se sabe.

    Un feed que corresponde a la página (portada con su feed, sección con el feed de la sección) da valores altos.
    Un feed genérico del sitio enlazado desde una página más específica (p. ej. el feed de todo Deportes en la
    página de un solo equipo) da valores bajos."""
    if not permitido_por_robots(url_del_feed):
        return None
    respuesta = peticion(url_del_feed)
    if not _parece_feed(respuesta):
        return None
    recientes = _enlaces_de_las_entradas_del_feed(respuesta.texto)[:ENTRADAS_RECIENTES_A_COMPARAR]
    if not recientes:
        return None
    en_la_pagina = {
        _enlace_comparable(urllib.parse.urljoin(url_pagina, enlace))
        for enlace in re.findall(r"href=[\"']([^\"'#]+)", html)
    }
    return sum(1 for enlace in recientes if enlace in en_la_pagina) / len(recientes)


def _ambito_de_la_pagina_aparece_en_el_feed(url_pagina, url_del_feed):
    """True si la página es una portada (sin ruta) o el último tramo de su ruta aparece en la URL del feed.

    /espana/ y /rss/2.0/espana/ comparten ámbito; /deportes/alfinaldelapalmera/ y /rss/2.0/deportes/ no."""
    segmentos = [segmento for segmento in urllib.parse.urlsplit(url_pagina).path.split("/") if segmento]
    if not segmentos:
        return True
    ultimo = re.sub(r"\.[a-z0-9]{2,5}$", "", segmentos[-1].lower())
    return len(ultimo) >= 3 and ultimo in url_del_feed.lower()


def feed_corresponde_a_la_pagina(html, url_pagina, url_del_feed):
    """True si el feed descubierto sirve para esta página. Ante la duda (no se puede medir), True: se respeta el feed.

    Sirve si comparte ámbito con la página (ruta) o si sus entradas recientes están enlazadas en ella. Un feed
    genérico del sitio, enlazado desde la página de un solo tema, no cumple ninguna de las dos cosas."""
    if _ambito_de_la_pagina_aparece_en_el_feed(url_pagina, url_del_feed):
        return True
    proporcion = proporcion_del_feed_en_la_pagina(html, url_pagina, url_del_feed)
    return proporcion is None or proporcion >= UMBRAL_FEED_DE_LA_PAGINA


def _etiqueta_sin_espacio_de_nombres(elemento):
    return elemento.tag.split("}")[-1].lower() if isinstance(elemento.tag, str) else ""


def _texto_sin_html(texto):
    sin_etiquetas = re.sub(r"<[^>]+>", " ", html.unescape(texto or ""))
    return " ".join(sin_etiquetas.split())


def _fecha_de_feed(texto):
    if not texto or not texto.strip():
        return None
    try:
        fecha = parsedate_to_datetime(texto.strip())
    except (TypeError, ValueError):
        fecha = interpretar_fecha(texto)
        if fecha is None:
            try:
                fecha = datetime.fromisoformat(texto.strip().replace("Z", "+00:00"))
            except ValueError:
                return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


MAXIMO_BYTES_DE_FEED = 5_000_000


def entradas_de_feed(texto_del_feed):
    """Entradas de un feed RSS o Atom como dicts {titulo, enlace, fecha, extracto}; lista vacía si no se puede leer.

    Se rechazan los feeds con declaraciones de entidades (<!ENTITY): son la vía de las bombas de expansión de XML."""
    if len(texto_del_feed) > MAXIMO_BYTES_DE_FEED or "<!ENTITY" in texto_del_feed:
        return []
    try:
        raiz = ElementTree.fromstring(texto_del_feed.encode("utf-8"))
    except ElementTree.ParseError:
        return []
    entradas = []
    for elemento in raiz.iter():
        if _etiqueta_sin_espacio_de_nombres(elemento) not in ("item", "entry"):
            continue
        datos = {"titulo": "", "enlace": "", "fecha": None, "extracto": ""}
        for hijo in elemento:
            etiqueta = _etiqueta_sin_espacio_de_nombres(hijo)
            valor = (hijo.text or "").strip()
            if etiqueta == "title":
                datos["titulo"] = _texto_sin_html(valor)
            elif etiqueta == "link":
                datos["enlace"] = hijo.attrib.get("href", "").strip() or valor
            elif etiqueta in ("pubdate", "published", "updated", "date") and datos["fecha"] is None:
                datos["fecha"] = _fecha_de_feed(valor)
            elif etiqueta in ("description", "summary") and not datos["extracto"]:
                datos["extracto"] = _texto_sin_html(valor)[:400]
        if datos["titulo"] and datos["enlace"].startswith(("http://", "https://")):
            entradas.append(datos)
    return entradas


MINIMO_PALABRAS_DEL_SLUG = 5
MINIMO_LONGITUD_DEL_SLUG = 30
PATRON_MARCA_DE_TIEMPO_EN_URL = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?(?!\d)")
PATRON_SUFIJO_DE_SLUG = re.compile(r"-?(?:20\d{12}|20\d{10}|20\d{6})(?:-[a-z]{1,4})?$|_0_\d+$")


def _slug_de_articulo(url):
    """Último tramo de la ruta sin extensión ni identificadores finales, o '' si no parece el de un artículo."""
    ruta = urllib.parse.urlsplit(url).path.rstrip("/")
    ultimo = re.sub(r"\.(?:html?|php|aspx?)$", "", ruta.rsplit("/", 1)[-1].lower())
    ultimo = PATRON_SUFIJO_DE_SLUG.sub("", ultimo)
    palabras = [palabra for palabra in ultimo.split("-") if palabra]
    if len(palabras) < MINIMO_PALABRAS_DEL_SLUG or len(ultimo) < MINIMO_LONGITUD_DEL_SLUG:
        return ""
    return ultimo


def _zona_horaria(nombre):
    """ZoneInfo de ese nombre («Europe/Madrid»), o UTC si no se indica o el sistema no la conoce."""
    if not nombre:
        return timezone.utc
    try:
        return ZoneInfo(nombre)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _fecha_de_la_url(url, nombre_de_zona=None):
    """Fecha de la marca de tiempo de la dirección, en la zona horaria de la web (las webs la suelen poner en hora local)."""
    coincidencia = PATRON_MARCA_DE_TIEMPO_EN_URL.search(urllib.parse.urlsplit(url).path)
    if not coincidencia:
        return None
    anio, mes, dia, hora, minuto, segundo = (int(valor or 0) for valor in coincidencia.groups())
    try:
        return datetime(anio, mes, dia, hora, minuto, segundo, tzinfo=_zona_horaria(nombre_de_zona))
    except ValueError:
        return None


def entradas_por_enlaces(html, url_base, zona_horaria_de_las_urls=None):
    """Alternativa a las tarjetas: todos los enlaces de la página que parecen artículos.

    Un enlace cuenta si es de la misma web y su último tramo es un «slug» largo (5 palabras o más separadas por guiones).
    El título es el mejor texto de enlace visto; si no hay, se saca de la propia dirección. La fecha, si la dirección
    lleva una marca de tiempo (20261031234026). Útil cuando el listado está repartido en bloques de estructuras distintas."""
    anfitrion = _host_sin_www(url_base)
    por_enlace = {}
    for nodo in descendientes(construir_arbol(html)):
        if not _es_enlace_util(nodo):
            continue
        enlace = urllib.parse.urljoin(url_base, nodo.atributos["href"].strip()).split("#")[0]
        if not enlace.startswith(("http://", "https://")) or _host_sin_www(enlace) != anfitrion:
            continue
        slug = _slug_de_articulo(enlace)
        if not slug:
            continue
        texto_del_enlace = texto_de(nodo)
        entrada = por_enlace.setdefault(enlace, {"titulo": "", "enlace": enlace, "fecha": _fecha_de_la_url(enlace, zona_horaria_de_las_urls),
                                                  "extracto": "", "slug": slug})
        if len(texto_del_enlace) > len(entrada["titulo"]) and len(texto_del_enlace.split()) >= 3:
            entrada["titulo"] = texto_del_enlace[:200]
    entradas = []
    for entrada in por_enlace.values():
        if not entrada["titulo"]:
            entrada["titulo"] = entrada["slug"].replace("-", " ").capitalize()
        del entrada["slug"]
        entradas.append(entrada)
    entradas.sort(key=lambda e: e["fecha"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return entradas


# --------------------------------------------------------------------------- análisis completo

def validar_url(url):
    """Devuelve un motivo de rechazo o '' si la URL es aceptable."""
    partes = urllib.parse.urlsplit(url)
    if partes.scheme != "https":
        return "solo se admiten URLs https"
    host = partes.hostname or ""
    if not host or "." not in host or host == "localhost":
        return "host no válido"
    if re.fullmatch(r"[\d.]+|\[.*\]", host):
        return "no se admiten direcciones IP"
    if partes.port not in (None, 443):
        return "no se admiten puertos distintos de 443"
    if partes.username or partes.password:
        return "no se admiten credenciales en la URL"
    return ""


def analizar(url):
    """Aplica los dos niveles y devuelve un dict con el resultado, siempre con la misma estructura."""
    resultado = {
        "url": url, "feed_existente": "", "entradas_feed": 0,
        "tarjetas": 0, "con_fechas": 0, "firma": "", "confianza": "", "muestra_titulos": [],
        "motivo_fallo": "", "entradas": [],
    }
    motivo_url = validar_url(url)
    if motivo_url:
        resultado["motivo_fallo"] = motivo_url
        return resultado

    respuesta, bloqueo = descargar_respetuosamente(url)
    if bloqueo:
        resultado["motivo_fallo"] = bloqueo
        return resultado
    if not (200 <= respuesta.estado < 300):
        resultado["motivo_fallo"] = respuesta.error or f"HTTP {respuesta.estado}"
        return resultado
    if "html" not in respuesta.tipo_contenido.lower() and "<html" not in respuesta.texto[:2000].lower():
        resultado["motivo_fallo"] = f"no es una página HTML ({respuesta.tipo_contenido or 'sin tipo'})"
        return resultado

    feed, cantidad = descubrir_feed(respuesta.texto, respuesta.url_final)
    if feed:
        resultado["feed_existente"] = feed
        resultado["entradas_feed"] = cantidad

    deteccion = detectar_tarjetas(respuesta.texto, respuesta.url_final)
    if deteccion:
        entradas = deteccion["entradas"][:MAXIMO_ENTRADAS]
        resultado["tarjetas"] = len(entradas)
        resultado["con_fechas"] = sum(1 for e in entradas if e["fecha"])
        resultado["firma"] = deteccion["firma"]
        resultado["confianza"] = deteccion["confianza"]
        resultado["muestra_titulos"] = [e["titulo"][:80] for e in entradas[:3]]
        resultado["entradas"] = entradas
    elif not feed:
        resultado["motivo_fallo"] = "sin feed y sin listado de tarjetas reconocible (¿página con JavaScript?)"
    return resultado


# --------------------------------------------------------------------------- salida RSS

# --------------------------------------------------------------------------- texto completo del artículo

ETIQUETAS_DEL_CUERPO = {
    "p", "h2", "h3", "h4", "ul", "ol", "li", "blockquote", "strong", "b", "em", "i", "br", "a", "img", "pre", "code",
}
ETIQUETAS_OMITIDAS = {"nav", "header", "footer", "aside", "form", "button", "select", "iframe", "figcaption"}
PATRON_RUIDO = re.compile(
    r"(^|[\s_-])(share|sharing|social|related|comments?|newsletter|subscribe|subscription|promo|advert|ad|ads|"
    r"sidebar|breadcrumb|cookie|cookies|menu|nav|navbar|footer|toolbar|tags?)($|[\s_-])",
    re.IGNORECASE,
)
MINIMO_CARACTERES_CUERPO = 400
MINIMO_CARACTERES_PARRAFO = 40
NODOS_DE_PAGINA = {"#raiz", "html", "body"}


def _es_ruido(nodo, usar_clases):
    if nodo.etiqueta in ETIQUETAS_OMITIDAS:
        return True
    if usar_clases and nodo.etiqueta not in NODOS_DE_PAGINA:
        texto = " ".join(nodo.clases) + " " + (nodo.atributos.get("id") or "")
        return bool(PATRON_RUIDO.search(texto))
    return False


def _en_zona_de_ruido(nodo, usar_clases):
    actual = nodo
    while actual is not None:
        if _es_ruido(actual, usar_clases):
            return True
        actual = actual.padre
    return False


def _longitud_de_enlaces(nodo):
    return sum(len(texto_de(a)) for a in descendientes(nodo) if a.etiqueta == "a")


def _elegir_contenedor(raiz, usar_clases):
    """El contenedor cuyos párrafos suman más texto (el padre cuenta entero y el abuelo, la mitad)."""
    puntos = {}
    for nodo in descendientes(raiz):
        if nodo.etiqueta != "p" or _en_zona_de_ruido(nodo, usar_clases):
            continue
        texto = texto_de(nodo)
        if len(texto) < MINIMO_CARACTERES_PARRAFO or _longitud_de_enlaces(nodo) > 0.5 * len(texto):
            continue
        padre = nodo.padre
        abuelo = padre.padre if padre is not None else None
        for contenedor, factor in ((padre, 1.0), (abuelo, 0.5)):
            if contenedor is None:
                continue
            registro = puntos.setdefault(id(contenedor), [contenedor, 0.0])
            registro[1] += len(texto) * factor
    if not puntos:
        return None
    mejor = max(puntos.values(), key=lambda registro: registro[1])[0]
    for antepasado in [mejor] + list(antepasados(mejor)):
        if antepasado.etiqueta == "article":
            return antepasado
    return mejor


def _a_html_simple(nodo, url_base, usar_clases):
    if nodo.es_texto():
        return escape(nodo.contenido)
    if _es_ruido(nodo, usar_clases):
        return ""
    hijos = "".join(_a_html_simple(hijo, url_base, usar_clases) for hijo in nodo.hijos)
    if nodo.etiqueta == "br":
        return "<br/>"
    if nodo.etiqueta == "img":
        origen = urllib.parse.urljoin(url_base, nodo.atributos.get("src", ""))
        if origen.startswith(("http://", "https://")):
            return f'<img src="{escape(origen, {chr(34): "&quot;"})}" alt="{escape(nodo.atributos.get("alt", ""), {chr(34): "&quot;"})}"/>'
        return ""
    if nodo.etiqueta == "a":
        destino = urllib.parse.urljoin(url_base, nodo.atributos.get("href", ""))
        if destino.startswith(("http://", "https://")):
            return f'<a href="{escape(destino, {chr(34): "&quot;"})}">{hijos}</a>'
        return hijos
    if nodo.etiqueta in ETIQUETAS_DEL_CUERPO:
        return f"<{nodo.etiqueta}>{hijos}</{nodo.etiqueta}>"
    return hijos


def fecha_del_articulo(html, cuerpo=""):
    """Fecha de publicación (aaaa-mm-dd) del artículo, o ''.

    Primero los metadatos de la página; si no hay, la primera fecha de los 300 primeros caracteres del cuerpo
    (las notas de prensa suelen empezar con «CIUDAD, 26 de octubre de 2026»)."""
    patrones = (
        r'property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*property=["\']article:published_time',
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'<time[^>]+datetime=["\']([^"\']+)',
        r'itemprop=["\']datePublished["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']datePublished',
        r'<meta[^>]+name=["\'](?:date|pubdate|publishdate|dc\.date(?:\.issued)?)["\'][^>]*content=["\']([^"\']+)',
        r'<time[^>]*>\s*([^<]{6,40}?)\s*</time>',  # fecha como texto de <time> (puede llevar una clase «hidden»)
    )
    for patron in patrones:
        coincidencia = re.search(patron, html, re.IGNORECASE)
        if coincidencia:
            fecha = interpretar_fecha(coincidencia.group(1))
            if fecha:
                return fecha.strftime("%Y-%m-%d")
    if cuerpo:
        principio = " ".join(re.sub(r"<[^>]+>", " ", cuerpo).split())[:300]
        fecha = interpretar_fecha(principio)
        if fecha:
            return fecha.strftime("%Y-%m-%d")
    return ""


MINIMO_CARACTERES_EXTRACTO = 30
PATRON_CONTENEDOR_DE_ARTICULO = (
    r'<(?:div|section|article)[^>]+class="[^"]*(?:long-text|entry-content|post-content|article-body|article__body|'
    r'story-body|news-body|noticia-texto|texto-noticia|cuerpo-noticia|content-text)[^"]*"[^>]*>(.*?)</(?:div|section|article)>'
)
MAXIMO_CARACTERES_EXTRACTO = 400


def _titulos_de_la_pagina(html_del_articulo):
    """Textos de <title> y og:title, en minúsculas, para saber si una descripción solo repite el título."""
    titulos = []
    for patron in (r"<title[^>]*>(.*?)</title>",
                   r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)',
                   r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:title'):
        coincidencia = re.search(patron, html_del_articulo, re.IGNORECASE | re.DOTALL)
        if coincidencia:
            titulos.append(_texto_sin_html(coincidencia.group(1)).lower())
    return titulos


MARGEN_DE_CARACTERES_SOBRE_EL_TITULO = 15


def _repite_el_titulo(candidato, titulos):
    """True si la descripción es el título (o está dentro de él, o es el título con poco más)."""
    texto = candidato.lower().strip(" .:-")
    for titulo in titulos:
        if texto in titulo or (titulo in texto and len(texto) <= len(titulo) + MARGEN_DE_CARACTERES_SOBRE_EL_TITULO):
            return True
    return False


def extracto_del_articulo(html_del_articulo, cuerpo=""):
    """Descripción corta del artículo: los metadatos de la página (og:description, twitter:description, description) y,
    si no hay, el primer párrafo del cuerpo. Texto plano, o '' si no se encuentra nada."""
    patrones = (
        r'<meta[^>]+(?:property|name)=["\'](?:og:description|twitter:description|description)["\'][^>]*content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:description|twitter:description|description)["\']',
    )
    titulos = _titulos_de_la_pagina(html_del_articulo)
    for patron in patrones:
        for coincidencia in re.finditer(patron, html_del_articulo, re.IGNORECASE):
            candidato = _texto_sin_html(coincidencia.group(1))
            if len(candidato) >= MINIMO_CARACTERES_EXTRACTO and not _repite_el_titulo(candidato, titulos):
                return candidato[:MAXIMO_CARACTERES_EXTRACTO]
    for parrafo in re.findall(r"<p[^>]*>(.*?)</p>", cuerpo or "", re.IGNORECASE | re.DOTALL):
        candidato = _texto_sin_html(parrafo)
        if len(candidato) >= 2 * MINIMO_CARACTERES_EXTRACTO:
            return candidato[:MAXIMO_CARACTERES_EXTRACTO]
    # Webs que escriben el artículo con <div> en vez de <p>: el primer bloque de un contenedor con nombre de contenido.
    for coincidencia in re.finditer(PATRON_CONTENEDOR_DE_ARTICULO, html_del_articulo, re.IGNORECASE | re.DOTALL):
        candidato = _texto_sin_html(coincidencia.group(1))
        if len(candidato) >= 2 * MINIMO_CARACTERES_EXTRACTO:
            return candidato[:MAXIMO_CARACTERES_EXTRACTO]
    return ""


def extraer_cuerpo_articulo(html, url_base):
    """Devuelve el cuerpo del artículo como HTML simple, o '' si no se reconoce uno razonable."""
    raiz = construir_arbol(html)
    for usar_clases in (True, False):
        contenedor = _elegir_contenedor(raiz, usar_clases)
        if contenedor is None:
            continue
        cuerpo = re.sub(r"\s+", " ", _a_html_simple(contenedor, url_base, usar_clases)).strip()
        if len(re.sub(r"<[^>]+>", "", cuerpo)) >= MINIMO_CARACTERES_CUERPO:
            return cuerpo
    return ""


def construir_rss(titulo, url_pagina, entradas):
    ahora = format_datetime(datetime.now(timezone.utc))
    lineas = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">',
        "<channel>",
        f"<title>{escape(titulo)}</title>",
        f"<link>{escape(url_pagina)}</link>",
        f"<description>{escape('Feed no oficial generado por Feedmaker a partir de ' + url_pagina)}</description>",
        f"<lastBuildDate>{ahora}</lastBuildDate>",
    ]
    for entrada in entradas:
        lineas += [
            "<item>",
            f"<title>{escape(entrada['titulo'])}</title>",
            f"<link>{escape(entrada['enlace'])}</link>",
            f'<guid isPermaLink="true">{escape(entrada["enlace"])}</guid>',
        ]
        if entrada["fecha"]:
            lineas.append(f"<pubDate>{format_datetime(entrada['fecha'])}</pubDate>")
        resumen = entrada["extracto"]
        if not resumen and entrada.get("cuerpo"):
            # Algunos lectores solo muestran <description>: sin extracto, se usa el principio del cuerpo.
            resumen = " ".join(re.sub(r"<[^>]+>", " ", entrada["cuerpo"]).split())
        if not resumen:
            # Sin <description>, Feedly muestra «[no content]»: en último caso, el propio título.
            resumen = entrada["titulo"]
        lineas.append(f"<description>{escape(resumen[:400])}</description>")
        if entrada.get("cuerpo"):
            cuerpo_seguro = entrada["cuerpo"].replace("]]>", "]]&gt;")
            lineas.append(f"<content:encoded><![CDATA[{cuerpo_seguro}]]></content:encoded>")
        lineas.append("</item>")
    lineas += ["</channel>", "</rss>"]
    return "\n".join(lineas) + "\n"
