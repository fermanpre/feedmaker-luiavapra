"""Alta de una web desde una incidencia de GitHub (título «alta: <url>»), pensada para el workflow alta.yml.

Uso:
  python alta.py registrar feeds.json      # lee ISSUE_TITLE, añade la web a feeds.json y escribe GITHUB_OUTPUT
  python alta.py comentario <id> docs/estado.json   # imprime el comentario (Markdown) para la incidencia

El título de la incidencia es una entrada no fiable: solo se acepta una URL https válida (extractor.validar_url)
y nunca se interpola en comandos de shell; llega por variable de entorno.
"""

import json
import os
import re
import sys
import urllib.parse

import extractor

URL_FEEDLY = "https://feedly.com/i/subscription/feed/"

# Combinados: límites pensados para uso personal; el servidor los aplica aunque la página ya los compruebe.
MINIMO_DE_FEEDS_POR_COMBINADO = 2
MAXIMO_DE_FEEDS_POR_COMBINADO = 5
MAXIMO_DE_COMBINADOS = 20
MAXIMO_DE_PALABRAS_DE_FILTRO = 5
MAXIMA_LONGITUD_DE_PALABRA = 40
MAXIMA_LONGITUD_DEL_NOMBRE = 80
PATRON_DE_IDENTIFICADOR = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def extraer_url(titulo):
    coincidencia = re.match(r"^\s*alta:\s*(\S+)\s*$", titulo or "", re.IGNORECASE)
    return coincidencia.group(1) if coincidencia else ""


def texto_completo_pedido():
    """True o False si el alta lo dice expresamente (parámetro TEXTO_COMPLETO del workflow o línea «texto_completo: true»
    / «texto_completo: false» en el cuerpo de la incidencia); None si no dice nada: entonces vale el valor por defecto
    de generar.py (texto completo). Solo se aceptan esos valores exactos; el resto de la entrada no se interpreta."""
    parametro = os.environ.get("TEXTO_COMPLETO", "").strip().lower()
    if parametro in ("true", "false"):
        return parametro == "true"
    cuerpo = os.environ.get("ISSUE_BODY", "")
    coincidencia = re.search(r"^\s*texto_completo:\s*(true|false)\s*$", cuerpo, re.IGNORECASE | re.MULTILINE)
    return coincidencia.group(1).lower() == "true" if coincidencia else None


def normalizar_url(url):
    """Forma canónica para detectar duplicados: sin «www.», sin barra final, sin fragmento ni parámetros de seguimiento.

    No sirve para descargar: la URL guardada en feeds.json es la que escribió el usuario."""
    partes = urllib.parse.urlsplit(url.strip())
    host = (partes.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    ruta = re.sub(r"/+$", "", partes.path) or ""
    parametros = [
        (clave, valor) for clave, valor in urllib.parse.parse_qsl(partes.query, keep_blank_values=True)
        if not clave.lower().startswith("utm_") and clave.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}
    ]
    consulta = urllib.parse.urlencode(sorted(parametros))
    return f"{host}{ruta}" + (f"?{consulta}" if consulta else "")


def crear_identificador(url, identificadores_en_uso):
    partes = urllib.parse.urlsplit(url)
    texto = (partes.netloc + partes.path).lower()
    texto = re.sub(r"^www\.", "", texto)
    base = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")[:50].strip("-") or "web"
    candidato = base
    contador = 2
    while candidato in identificadores_en_uso:
        candidato = f"{base}-{contador}"
        contador += 1
    return candidato


def escribir_salida(**valores):
    ruta = os.environ.get("GITHUB_OUTPUT")
    lineas = [f"{clave}={valor}" for clave, valor in valores.items()]
    if ruta:
        with open(ruta, "a", encoding="utf-8") as fichero:
            fichero.write("\n".join(lineas) + "\n")
    print("\n".join(lineas))


def limpiar_texto_libre(texto, maximo):
    """Texto de una sola línea, sin caracteres de control, con los espacios normalizados y recortado."""
    sin_control = re.sub(r"[\x00-\x1f\x7f]", " ", texto or "")
    return " ".join(sin_control.split())[:maximo]


def separar_por_comas(texto):
    """Lista sin vacíos ni repetidos (sin distinguir mayúsculas), en el orden en que aparecen."""
    vistos = set()
    elementos = []
    for trozo in (texto or "").split(","):
        elemento = " ".join(trozo.split())
        if elemento and elemento.lower() not in vistos:
            vistos.add(elemento.lower())
            elementos.append(elemento)
    return elementos


def leer_peticion_de_combinado():
    """(identificadores, nombre, filtro) si el alta pide un combinado, o None si pide una web.

    Llega por los parámetros del workflow (COMBINA, NOMBRE_COMBINADO, FILTRO_COMBINADO) o por una incidencia con el
    título «combinar: id1, id2, id3» y, en el cuerpo, líneas opcionales «nombre: …» y «filtro: palabra1, palabra2»."""
    combina = os.environ.get("COMBINA", "").strip()
    nombre = os.environ.get("NOMBRE_COMBINADO", "")
    filtro = os.environ.get("FILTRO_COMBINADO", "")
    if not combina:
        coincidencia = re.match(r"^\s*combinar:\s*(.+?)\s*$", os.environ.get("ISSUE_TITLE", ""), re.IGNORECASE)
        if not coincidencia:
            return None
        combina = coincidencia.group(1)
        cuerpo = os.environ.get("ISSUE_BODY", "")
        linea_nombre = re.search(r"^\s*nombre:\s*(.*?)\s*$", cuerpo, re.IGNORECASE | re.MULTILINE)
        linea_filtro = re.search(r"^\s*filtro:\s*(.*?)\s*$", cuerpo, re.IGNORECASE | re.MULTILINE)
        nombre = linea_nombre.group(1) if linea_nombre else ""
        filtro = linea_filtro.group(1) if linea_filtro else ""
    return separar_por_comas(combina), limpiar_texto_libre(nombre, MAXIMA_LONGITUD_DEL_NOMBRE), separar_por_comas(filtro)


def validar_combinado(identificadores, filtro, sitios):
    """Motivo de rechazo, o '' si el combinado es aceptable."""
    if not MINIMO_DE_FEEDS_POR_COMBINADO <= len(identificadores) <= MAXIMO_DE_FEEDS_POR_COMBINADO:
        return (f"un combinado lleva de {MINIMO_DE_FEEDS_POR_COMBINADO} a {MAXIMO_DE_FEEDS_POR_COMBINADO} feeds "
                f"(se han indicado {len(identificadores)})")
    por_identificador = {sitio["id"]: sitio for sitio in sitios}
    for identificador in identificadores:
        if not PATRON_DE_IDENTIFICADOR.match(identificador):
            return "identificador de feed no válido"
        miembro = por_identificador.get(identificador)
        if miembro is None:
            return f"el feed «{identificador}» no existe"
        if "combina" in miembro:
            return f"«{identificador}» es un combinado: no se pueden anidar"
    if len(filtro) > MAXIMO_DE_PALABRAS_DE_FILTRO:
        return f"el filtro admite hasta {MAXIMO_DE_PALABRAS_DE_FILTRO} palabras"
    if any(len(palabra) > MAXIMA_LONGITUD_DE_PALABRA or re.search(r"[\x00-\x1f\x7f]", palabra) for palabra in filtro):
        return f"cada palabra del filtro admite hasta {MAXIMA_LONGITUD_DE_PALABRA} caracteres"
    return ""


def registrar_combinado(ruta_feeds, identificadores, nombre, filtro):
    with open(ruta_feeds, encoding="utf-8") as fichero:
        sitios = json.load(fichero)
    motivo = validar_combinado(identificadores, filtro, sitios)
    if motivo:
        escribir_salida(valida="false", motivo=motivo.replace("\n", " "), id="")
        return
    claves = (frozenset(identificadores), tuple(sorted(palabra.lower() for palabra in filtro)))
    existente = next((sitio for sitio in sitios if "combina" in sitio and (
        frozenset(sitio["combina"]), tuple(sorted(palabra.lower() for palabra in sitio.get("filtro", [])))) == claves), None)
    if existente is not None:
        escribir_salida(valida="true", motivo="", id=existente["id"])
        return
    if sum(1 for sitio in sitios if "combina" in sitio) >= MAXIMO_DE_COMBINADOS:
        escribir_salida(valida="false", motivo=f"ya hay {MAXIMO_DE_COMBINADOS} combinados: borra alguno antes", id="")
        return
    por_identificador = {sitio["id"]: sitio for sitio in sitios}
    if not nombre:
        nombres = [por_identificador[i].get("etiqueta") or por_identificador[i]["nombre"] for i in identificadores]
        nombre = limpiar_texto_libre("Combinado: " + " + ".join(nombres), MAXIMA_LONGITUD_DEL_NOMBRE)
    base = "comb-" + (re.sub(r"[^a-z0-9]+", "-", nombre.lower()).strip("-")[:40].strip("-") or "feeds")
    en_uso = {sitio["id"] for sitio in sitios}
    nuevo_id, contador = base, 2
    while nuevo_id in en_uso:
        nuevo_id = f"{base}-{contador}"
        contador += 1
    combinado = {"id": nuevo_id, "nombre": nombre, "combina": identificadores}
    if filtro:
        combinado["filtro"] = filtro
    pedido = texto_completo_pedido()
    if pedido is not None:
        combinado["texto_completo"] = pedido
    sitios.append(combinado)
    with open(ruta_feeds, "w", encoding="utf-8", newline="\n") as fichero:
        json.dump(sitios, fichero, ensure_ascii=False, indent=2)
        fichero.write("\n")
    escribir_salida(valida="true", motivo="", id=nuevo_id)


def registrar(ruta_feeds):
    peticion_de_combinado = leer_peticion_de_combinado()
    if peticion_de_combinado is not None:
        registrar_combinado(ruta_feeds, *peticion_de_combinado)
        return
    # La URL llega por el parámetro del workflow (URL_ALTA) o por el título de una incidencia.
    url = os.environ.get("URL_ALTA", "").strip() or extraer_url(os.environ.get("ISSUE_TITLE", ""))
    motivo = "no se ha recibido ninguna URL (el título debe ser «alta: <url>»)" if not url else extractor.validar_url(url)
    if motivo:
        escribir_salida(valida="false", motivo=motivo.replace("\n", " "), id="")
        return

    with open(ruta_feeds, encoding="utf-8") as fichero:
        sitios = json.load(fichero)
    existente = next((sitio for sitio in sitios if normalizar_url(sitio.get("url", "")) == normalizar_url(url)), None)
    if existente is None:
        identificador = crear_identificador(url, {sitio["id"] for sitio in sitios})
        partes = urllib.parse.urlsplit(url)
        sitio_nuevo = {"id": identificador, "nombre": partes.netloc + partes.path.rstrip("/"), "url": url}
        pedido = texto_completo_pedido()
        if pedido is not None:
            sitio_nuevo["texto_completo"] = pedido
        sitios.append(sitio_nuevo)
        with open(ruta_feeds, "w", encoding="utf-8", newline="\n") as fichero:
            json.dump(sitios, fichero, ensure_ascii=False, indent=2)
            fichero.write("\n")
    else:
        identificador = existente["id"]
    escribir_salida(valida="true", motivo="", id=identificador)


def enlace_feedly(url_del_feed):
    return URL_FEEDLY + urllib.parse.quote(url_del_feed, safe="")


def comentario(identificador, ruta_estado):
    repositorio = os.environ.get("GITHUB_REPOSITORY", "usuario/repositorio")
    propietario, nombre = repositorio.split("/", 1)
    base = f"https://{propietario}.github.io/{nombre}/"
    with open(ruta_estado, encoding="utf-8") as fichero:
        sitios = json.load(fichero)["sitios"]
    sitio = next((s for s in sitios if s["id"] == identificador), None)
    if sitio is None:
        return "No se ha encontrado la web en el estado. Revisa el workflow."
    if sitio["estado"] == "ok":
        feed = f"{base}feeds/{identificador}.xml"
        return (f"Listo: {sitio['entradas']} entradas.\n\n"
                f"- Feed: {feed}\n- Añadir a Feedly: {enlace_feedly(feed)}\n\n"
                "Pages puede tardar 1-2 minutos en servir el fichero nuevo.")
    if sitio["estado"] == "feed_propio":
        return (f"Esta web ya tiene feed propio: {sitio['feed_propio']}\n\n"
                f"Añadir a Feedly: {enlace_feedly(sitio['feed_propio'])}")
    mensaje = f"No se ha podido generar el feed: {sitio['detalle']}"
    if sitio.get("sugerencias"):
        mensaje += "\n\nPrueba con alguna de estas direcciones, que parecen listados:\n" + "\n".join(
            f"- {direccion}" for direccion in sitio["sugerencias"])
    return mensaje


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "registrar":
        registrar(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "comentario":
        print(comentario(sys.argv[2], sys.argv[3]))
    else:
        raise SystemExit(__doc__)
