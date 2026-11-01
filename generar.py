"""Genera los feeds RSS de las webs listadas en feeds.json.

Uso: python generar.py [ruta/a/feeds.json]
Las salidas se escriben junto al feeds.json: docs/feeds/<id>.xml, docs/estado.json y datos/decisiones.json.

Por cada web: descarga la página, mira si ya tiene feed propio y, si no, detecta el listado de
artículos. Con clave de API, Haiku 5.5 elige el listado y marca las entradas que no son artículos;
sin clave se usa el mejor candidato sin filtrar. Las decisiones se guardan para no repetir llamadas.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import extractor
import ia

USER_AGENT_NAVEGADOR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CODIGOS_DE_BLOQUEO = (403, 406)
VERSION_CACHE_ARTICULOS = 2  # súbela cuando cambie la extracción del cuerpo o de la fecha
MAXIMO_ENTRADAS = 50
MAXIMO_CANDIDATOS = 5
TOPE_LLAMADAS_IA = int(os.environ.get("FEEDMAKER_MAX_LLAMADAS", "60"))


class Presupuesto:
    """Cuenta las llamadas a la IA de esta ejecución y corta al llegar al tope."""

    def __init__(self, tope):
        self.tope = tope
        self.llamadas = 0
        self.tokens_entrada = 0
        self.tokens_salida = 0

    def hay_margen(self):
        return self.llamadas < self.tope

    def registrar(self, entrada, salida):
        self.llamadas += 1
        self.tokens_entrada += entrada
        self.tokens_salida += salida


def leer_json(ruta, valor_por_defecto):
    if not os.path.exists(ruta):
        return valor_por_defecto
    with open(ruta, encoding="utf-8") as fichero:
        return json.load(fichero)


def escribir_json(ruta, datos):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    temporal = ruta + ".tmp"
    with open(temporal, "w", encoding="utf-8", newline="\n") as fichero:
        json.dump(datos, fichero, ensure_ascii=False, indent=1, sort_keys=True)
        fichero.write("\n")
    os.replace(temporal, ruta)


def escribir_texto(ruta, texto):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    temporal = ruta + ".tmp"
    with open(temporal, "w", encoding="utf-8", newline="\n") as fichero:
        fichero.write(texto)
    os.replace(temporal, ruta)


def firma_sin_cantidad(firma):
    """La firma lleva «(xN)» con el número de elementos, que cambia cada día."""
    return re.sub(r"\s*\(x\d+\)\s*$", "", firma)


def elegir_grupo(sitio, candidatos, decisiones_del_sitio, cliente, presupuesto):
    """Devuelve (candidato_elegido_o_None, motivo_si_ninguno)."""
    firma_guardada = decisiones_del_sitio.get("firma")
    if firma_guardada:
        for candidato in candidatos:
            if firma_sin_cantidad(candidato["firma"]) == firma_guardada:
                return candidato, ""
    if cliente is None or not presupuesto.hay_margen():
        return candidatos[0], ""
    indice, entrada, salida = ia.elegir_candidato(cliente, sitio["url"], candidatos)
    presupuesto.registrar(entrada, salida)
    if indice is None:
        return None, "el modelo no reconoce ningún listado de artículos"
    return candidatos[indice], ""


def filtrar_entradas(sitio, entradas, decisiones_del_sitio, cliente, presupuesto):
    """Aplica las decisiones guardadas y pide al modelo solo las de enlaces nuevos."""
    por_enlace = decisiones_del_sitio.setdefault("enlaces", {})
    nuevas = [entrada for entrada in entradas if entrada["enlace"] not in por_enlace]
    if nuevas and cliente is not None and presupuesto.hay_margen():
        descartar, entrada_tokens, salida_tokens = ia.entradas_a_descartar(cliente, sitio["url"], nuevas)
        presupuesto.registrar(entrada_tokens, salida_tokens)
        for posicion, entrada in enumerate(nuevas):
            por_enlace[entrada["enlace"]] = "descartar" if posicion in descartar else "ok"
    return [entrada for entrada in entradas if por_enlace.get(entrada["enlace"]) != "descartar"]


def completar_con_texto(entradas, articulos, descargas):
    """Añade a cada entrada el cuerpo del artículo, descargando solo los que no están en caché.

    `descargas` es un dict {"hechas": n, "tope": m} compartido por todas las webs de la ejecución.
    Si un artículo no se puede descargar o no se reconoce su cuerpo, la entrada sale solo con el extracto.
    """
    for entrada in entradas:
        enlace = entrada["enlace"]
        # Las entradas de una versión anterior de la caché se vuelven a descargar una vez (VERSION_CACHE_ARTICULOS).
        if articulos.get(enlace, {}).get("v") != VERSION_CACHE_ARTICULOS and descargas["hechas"] < descargas["tope"]:
            descargas["hechas"] += 1
            cuerpo = ""
            fecha = ""
            if extractor.permitido_por_robots(enlace):
                respuesta = extractor.peticion(enlace)
                if 200 <= respuesta.estado < 300:
                    cuerpo = extractor.extraer_cuerpo_articulo(respuesta.texto, respuesta.url_final)
                    fecha = extractor.fecha_del_articulo(respuesta.texto, cuerpo)
            articulos[enlace] = {"cuerpo": cuerpo, "fecha": fecha, "v": VERSION_CACHE_ARTICULOS}
        entrada["cuerpo"] = articulos.get(enlace, {}).get("cuerpo", "")
        if not entrada["fecha"] and articulos.get(enlace, {}).get("fecha"):
            entrada["fecha"] = extractor.interpretar_fecha(articulos[enlace]["fecha"])
    return sum(1 for entrada in entradas if entrada["cuerpo"])


def procesar_sitio(sitio, raiz, decisiones, cliente, presupuesto, articulos, descargas):
    """Devuelve el estado público del sitio. Nunca lanza: un fallo no debe parar a los demás."""
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    estado = {"id": sitio["id"], "nombre": sitio["nombre"], "url": sitio["url"], "estado": "error",
              "detalle": "", "feed_propio": "", "entradas": 0, "user_agent": "feedmaker", "actualizado": ahora}
    try:
        # user_agent: "auto" (por defecto) prueba primero el honesto y reintenta como navegador solo si la
        # web responde 403/406; "honesto" nunca se presenta como navegador; "navegador" lo hace siempre.
        modo_user_agent = sitio.get("user_agent", "auto")
        usa_navegador = modo_user_agent == "navegador"
        extractor.CABECERA_USER_AGENT = USER_AGENT_NAVEGADOR if usa_navegador else extractor.AGENTE_USUARIO
        motivo_url = extractor.validar_url(sitio["url"])
        if motivo_url:
            estado["detalle"] = motivo_url
            return estado

        respuesta, bloqueo = extractor.descargar_respetuosamente(sitio["url"])
        if bloqueo:
            estado["detalle"] = bloqueo
            return estado
        if respuesta.estado in CODIGOS_DE_BLOQUEO and modo_user_agent == "auto":
            extractor.CABECERA_USER_AGENT = USER_AGENT_NAVEGADOR
            usa_navegador = True
            respuesta = extractor.peticion(sitio["url"])
        estado["user_agent"] = "navegador" if usa_navegador else "feedmaker"
        if not (200 <= respuesta.estado < 300):
            estado["detalle"] = respuesta.error or f"HTTP {respuesta.estado}"
            return estado

        feed_propio, _ = extractor.descubrir_feed(respuesta.texto, respuesta.url_final)
        if feed_propio and not sitio.get("forzar_tarjetas"):
            estado["estado"] = "feed_propio"
            estado["feed_propio"] = feed_propio
            estado["detalle"] = "la web ya tiene feed: suscríbete a ese"
            return estado

        candidatos = extractor.detectar_candidatos(respuesta.texto, respuesta.url_final, MAXIMO_CANDIDATOS)
        if not candidatos:
            estado["detalle"] = "sin listado de artículos reconocible (¿página con JavaScript?)"
            return estado

        decisiones_del_sitio = decisiones.setdefault(sitio["id"], {})
        elegido, motivo = elegir_grupo(sitio, candidatos, decisiones_del_sitio, cliente, presupuesto)
        if elegido is None:
            estado["detalle"] = motivo
            return estado
        if cliente is not None:
            decisiones_del_sitio["firma"] = firma_sin_cantidad(elegido["firma"])

        entradas = filtrar_entradas(sitio, elegido["entradas"][:MAXIMO_ENTRADAS], decisiones_del_sitio,
                                    cliente, presupuesto)
        if not entradas:
            estado["detalle"] = "tras filtrar no queda ninguna entrada"
            return estado

        con_texto = 0
        if sitio.get("texto_completo"):
            con_texto = completar_con_texto(entradas, articulos, descargas)

        ruta_feed = os.path.join(raiz, "docs", "feeds", sitio["id"] + ".xml")
        escribir_texto(ruta_feed, extractor.construir_rss(sitio["nombre"], respuesta.url_final, entradas))
        estado["estado"] = "ok"
        estado["entradas"] = len(entradas)
        estado["con_texto_completo"] = con_texto
        estado["detalle"] = "con IA" if cliente is not None else "sin IA (no hay clave de API)"
        return estado
    except Exception as error:
        estado["detalle"] = f"error interno: {type(error).__name__}: {error}"
        return estado


def main():
    ruta_sitios = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "feeds.json")
    raiz = os.path.dirname(ruta_sitios)
    sitios = leer_json(ruta_sitios, [])
    if not sitios:
        raise SystemExit(f"ERROR: {ruta_sitios} no existe o está vacío.")

    ruta_decisiones = os.path.join(raiz, "datos", "decisiones.json")
    ruta_estado = os.path.join(raiz, "docs", "estado.json")
    decisiones = leer_json(ruta_decisiones, {})
    ruta_articulos = os.path.join(raiz, "datos", "articulos.json")
    articulos = leer_json(ruta_articulos, {})
    descargas = {"hechas": 0, "tope": int(os.environ.get("FEEDMAKER_MAX_ARTICULOS", "15"))}
    estado_anterior = {entrada["id"]: entrada for entrada in leer_json(ruta_estado, {}).get("sitios", [])}

    cliente = ia.crear_cliente()
    presupuesto = Presupuesto(TOPE_LLAMADAS_IA)
    print("IA:", "activada (Haiku 5.5)" if cliente else "desactivada: no hay ANTHROPIC_API_KEY", flush=True)

    resultados = []
    for sitio in sitios:
        estado = procesar_sitio(sitio, raiz, decisiones, cliente, presupuesto, articulos, descargas)
        anterior = estado_anterior.get(sitio["id"])
        if estado["estado"] == "error" and anterior and anterior["estado"] == "ok":
            # El feed anterior sigue publicado: se conserva su número de entradas y se anota el fallo.
            estado["entradas"] = anterior["entradas"]
            estado["detalle"] = "no se pudo actualizar (se mantiene el feed anterior): " + estado["detalle"]
        resultados.append(estado)
        print(f"- {sitio['id']}: {estado['estado']} ({estado['entradas']} entradas) {estado['detalle']}", flush=True)

    escribir_json(ruta_decisiones, decisiones)
    if articulos:
        escribir_json(ruta_articulos, articulos)
    escribir_json(ruta_estado, {
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "llamadas_ia": presupuesto.llamadas,
        "sitios": resultados,
    })
    print(f"Llamadas a la IA: {presupuesto.llamadas} (tokens {presupuesto.tokens_entrada} entrada / "
          f"{presupuesto.tokens_salida} salida)")
    if all(estado["estado"] == "error" for estado in resultados):
        raise SystemExit("ERROR: ninguna web se ha podido procesar.")


if __name__ == "__main__":
    main()
