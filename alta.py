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


def extraer_url(titulo):
    coincidencia = re.match(r"^\s*alta:\s*(\S+)\s*$", titulo or "", re.IGNORECASE)
    return coincidencia.group(1) if coincidencia else ""


def pide_texto_completo():
    """True si el alta pide el texto completo: parámetro TEXTO_COMPLETO del workflow o línea «texto_completo: true»
    en el cuerpo de la incidencia. Solo se acepta ese valor exacto; el resto de la entrada no se interpreta."""
    if os.environ.get("TEXTO_COMPLETO", "").strip().lower() == "true":
        return True
    cuerpo = os.environ.get("ISSUE_BODY", "")
    return re.search(r"^\s*texto_completo:\s*true\s*$", cuerpo, re.IGNORECASE | re.MULTILINE) is not None


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


def registrar(ruta_feeds):
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
        if pide_texto_completo():
            sitio_nuevo["texto_completo"] = True
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
    return f"No se ha podido generar el feed: {sitio['detalle']}"


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "registrar":
        registrar(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "comentario":
        print(comentario(sys.argv[2], sys.argv[3]))
    else:
        raise SystemExit(__doc__)
