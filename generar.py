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
from datetime import date, datetime, time, timezone

import extractor
import ia

USER_AGENT_NAVEGADOR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CODIGOS_DE_BLOQUEO = (403, 406)
VERSION_CACHE_ARTICULOS = 6  # súbela cuando cambie la extracción del cuerpo o de la fecha
MAXIMO_ENTRADAS = 50
CONSERVAR_DIAS_POR_DEFECTO = 180  # campo «conservar_dias» de cada web; 0 = solo lo que enseña hoy la web
MAXIMO_ENTRADAS_EN_FEED = 300
MAXIMO_ENTRADAS_POR_ENLACES = 100
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


def _fecha_de_registro(texto_iso):
    if not texto_iso:
        return None
    fecha = datetime.fromisoformat(texto_iso)
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


def quiere_texto_completo(sitio):
    """El texto completo es lo normal; se desactiva con `texto_completo: false`. Las webs `oculto` (ingredientes de un
    combinado) no lo descargan por su cuenta: el combinado lo descarga solo de las entradas que pasan su filtro."""
    return sitio.get("texto_completo", not sitio.get("oculto"))


def entrada_desde_registro(enlace, registro, articulos, con_texto_completo):
    entrada = {"titulo": registro["titulo"], "enlace": enlace, "fecha": _fecha_de_registro(registro["fecha"]),
               "extracto": registro["extracto"]}
    if con_texto_completo:
        entrada["cuerpo"] = articulos.get(enlace, {}).get("cuerpo", "")
    return entrada


def momento_de_orden(fecha, visto_iso):
    """Las entradas se ordenan por su fecha; si no tienen, por el día en que se vieron por primera vez."""
    if fecha:
        return fecha
    return datetime.combine(date.fromisoformat(visto_iso), time.min, tzinfo=timezone.utc)


FRACCION_CON_FECHA_SUFICIENTE = 0.6
INTENTOS_SIN_EXITO_PARA_RENDIRSE = 3


def completar_fechas(entradas, articulos, descargas, decisiones_del_sitio):
    """Si a muchas entradas les falta la fecha, la busca entrando en cada artículo (sin guardar su texto).

    Solo actúa si menos del 60 % de las entradas tienen fecha. Lo ya intentado se guarda en la caché de artículos,
    con éxito o sin él, para no repetir la descarga. Comparte con `completar_con_texto` el tope de descargas por
    ejecución. Si los primeros 3 artículos de una web no dan ninguna fecha, se deja de intentarlo en esa web.
    Devuelve cuántas entradas han ganado fecha."""
    if decisiones_del_sitio.get("fechas_en_articulos") is False:
        return 0
    sin_fecha = [entrada for entrada in entradas if not entrada["fecha"]]
    if len(entradas) - len(sin_fecha) >= FRACCION_CON_FECHA_SUFICIENTE * len(entradas):
        return 0
    conseguidas = 0
    intentos = 0
    for entrada in sin_fecha:
        enlace = entrada["enlace"]
        guardado = articulos.get(enlace)
        if guardado is not None and guardado.get("v") == VERSION_CACHE_ARTICULOS:
            if not entrada["extracto"]:
                entrada["extracto"] = guardado.get("extracto", "")
            fecha_guardada = extractor.interpretar_fecha(guardado.get("fecha", ""))
            if fecha_guardada:
                entrada["fecha"] = fecha_guardada
                conseguidas += 1
            continue  # ya se intentó: con fecha o sin ella, no se vuelve a descargar
        if descargas["hechas"] >= descargas["tope"]:
            continue
        descargas["hechas"] += 1
        intentos += 1
        _, texto_de_la_fecha, extracto = descargar_datos_del_articulo(enlace)
        articulos[enlace] = {"cuerpo": "", "fecha": texto_de_la_fecha, "extracto": extracto,
                             "v": VERSION_CACHE_ARTICULOS, "solo_fecha": True}
        if not entrada["extracto"]:
            entrada["extracto"] = extracto
        fecha = extractor.interpretar_fecha(texto_de_la_fecha)
        if fecha:
            entrada["fecha"] = fecha
            conseguidas += 1
        elif intentos >= INTENTOS_SIN_EXITO_PARA_RENDIRSE and conseguidas == 0:
            decisiones_del_sitio["fechas_en_articulos"] = False
            break
    return conseguidas


def completar_extractos(entradas, articulos, descargas):
    """Da extracto a las entradas que no lo tienen, desde la caché o entrando en el artículo (con el tope de descargas).

    Lo ya intentado se guarda con éxito o sin él. Si de paso se descarga una fecha que faltaba, también se aplica.
    Devuelve cuántas entradas han ganado extracto."""
    conseguidos = 0
    for entrada in entradas:
        if entrada["extracto"]:
            continue
        enlace = entrada["enlace"]
        guardado = articulos.get(enlace)
        if guardado is not None and "extracto" in guardado:
            if guardado["extracto"]:
                entrada["extracto"] = guardado["extracto"]
                conseguidos += 1
            continue  # ya se intentó, con extracto o sin él
        if descargas["hechas"] >= descargas["tope"]:
            continue
        descargas["hechas"] += 1
        cuerpo, texto_de_la_fecha, extracto = descargar_datos_del_articulo(enlace)
        if guardado is None:
            guardado = {"cuerpo": "", "fecha": texto_de_la_fecha, "v": VERSION_CACHE_ARTICULOS, "solo_fecha": True}
        guardado["extracto"] = extracto
        articulos[enlace] = guardado
        if extracto:
            entrada["extracto"] = extracto
            conseguidos += 1
        if not entrada["fecha"] and texto_de_la_fecha:
            entrada["fecha"] = extractor.interpretar_fecha(texto_de_la_fecha)
    return conseguidos


def fusionar_con_historial(historial_del_sitio, entradas_actuales, dias, hoy, articulos, con_texto_completo):
    """Mezcla lo que enseña hoy la web con lo que se vio en días anteriores y devuelve las entradas del feed.

    `historial_del_sitio` es {enlace: {titulo, fecha, extracto, visto}} y se modifica en el sitio. Una entrada se
    conserva mientras no lleve más de `dias` días sin aparecer en la web: lo que sigue listado no caduca nunca,
    sea cual sea su fecha de publicación. El cuerpo de las entradas que ya no están en la web sale de la caché de
    artículos, sin descargar nada; las que no están en la caché salen solo con el extracto.
    """
    hoy_dia = hoy.date()
    actuales_por_enlace = {entrada["enlace"]: entrada for entrada in entradas_actuales}
    for enlace, entrada in actuales_por_enlace.items():
        historial_del_sitio[enlace] = {
            "titulo": entrada["titulo"],
            "fecha": entrada["fecha"].isoformat() if entrada["fecha"] else "",
            "extracto": entrada["extracto"],
            "visto": hoy_dia.isoformat(),
        }
    for enlace in list(historial_del_sitio):
        dias_sin_verse = (hoy_dia - date.fromisoformat(historial_del_sitio[enlace]["visto"])).days
        if dias_sin_verse > dias:
            del historial_del_sitio[enlace]

    fusionadas = []
    for enlace, registro in historial_del_sitio.items():
        if enlace in actuales_por_enlace:
            entrada = actuales_por_enlace[enlace]
        else:
            entrada = entrada_desde_registro(enlace, registro, articulos, con_texto_completo)
        fusionadas.append(entrada)

    fusionadas.sort(
        key=lambda entrada: momento_de_orden(entrada["fecha"], historial_del_sitio[entrada["enlace"]]["visto"]),
        reverse=True)
    for sobrante in fusionadas[MAXIMO_ENTRADAS_EN_FEED:]:
        del historial_del_sitio[sobrante["enlace"]]
    return fusionadas[:MAXIMO_ENTRADAS_EN_FEED]


def descargar_datos_del_articulo(enlace):
    """(cuerpo, fecha, extracto) de un artículo; cadenas vacías si no se puede descargar o no se reconoce."""
    if not extractor.permitido_por_robots(enlace):
        return "", "", ""
    respuesta = extractor.peticion(enlace)
    if not (200 <= respuesta.estado < 300):
        return "", "", ""
    cuerpo = extractor.extraer_cuerpo_articulo(respuesta.texto, respuesta.url_final) or ""
    fecha = extractor.fecha_del_articulo(respuesta.texto, cuerpo) or ""
    extracto = extractor.extracto_del_articulo(respuesta.texto, cuerpo)
    return cuerpo, fecha, extracto


def completar_con_texto(entradas, articulos, descargas):
    """Añade a cada entrada el cuerpo del artículo, descargando solo los que no están en caché.

    `descargas` es un dict {"hechas": n, "tope": m} compartido por todas las webs de la ejecución.
    Si un artículo no se puede descargar o no se reconoce su cuerpo, la entrada sale solo con el extracto.
    """
    for entrada in entradas:
        enlace = entrada["enlace"]
        # Las entradas de una versión anterior de la caché se vuelven a descargar una vez (VERSION_CACHE_ARTICULOS).
        guardado = articulos.get(enlace, {})
        necesita_descarga = guardado.get("v") != VERSION_CACHE_ARTICULOS or guardado.get("solo_fecha")
        if necesita_descarga and descargas["hechas"] < descargas["tope"]:
            descargas["hechas"] += 1
            cuerpo, fecha, extracto = descargar_datos_del_articulo(enlace)
            articulos[enlace] = {"cuerpo": cuerpo, "fecha": fecha, "extracto": extracto, "v": VERSION_CACHE_ARTICULOS}
        guardado = articulos.get(enlace, {})
        entrada["cuerpo"] = guardado.get("cuerpo", "")
        if not entrada["fecha"] and guardado.get("fecha"):
            entrada["fecha"] = extractor.interpretar_fecha(guardado["fecha"])
        if not entrada["extracto"] and guardado.get("extracto"):
            entrada["extracto"] = guardado["extracto"]
    return sum(1 for entrada in entradas if entrada["cuerpo"])


FEED_GENERICO_SIN_ALTERNATIVA = ("el feed de la web es más general que esta página y no se ha podido generar uno "
                                 "propio de la página: suscríbete a ese")


def estado_con_feed_propio(estado, feed_propio, detalle):
    estado["estado"] = "feed_propio"
    estado["feed_propio"] = feed_propio
    estado["detalle"] = detalle
    return estado


def _normalizar_para_filtro(texto):
    return " ".join(texto.lower().replace("\xa0", " ").split())


def construir_combinado(combinado, sitios_por_id, historial, articulos):
    """Junta las entradas del historial de varias webs en un solo feed.

    Devuelve (entradas, avisos). Con `filtro` (lista de palabras o frases), solo pasan las entradas cuyo título o
    extracto o dirección (los pies de foto no suelen nombrar al equipo, la URL sí) contiene alguna como palabra completa («caja» no coincide con «Cajasol»). Una web puede sustituir ese
    filtro por el suyo con `filtro_en_combinado`. Cada título lleva delante el nombre de su web de origen."""
    palabras_generales = [_normalizar_para_filtro(palabra) for palabra in combinado.get("filtro", [])]
    entradas = []
    avisos = []
    for identificador in combinado["combina"]:
        miembro = sitios_por_id.get(identificador)
        registros = historial.get(identificador, {})
        if miembro is None:
            avisos.append(f"{identificador}: no existe en feeds.json")
            continue
        if not registros:
            avisos.append(f"{identificador}: sin entradas guardadas (¿tiene feed propio o falló?)")
            continue
        etiqueta = miembro.get("etiqueta") or miembro["nombre"]
        palabras = [_normalizar_para_filtro(palabra) for palabra in miembro.get("filtro_en_combinado", [])]             or palabras_generales
        for enlace, registro in registros.items():
            contenido = _normalizar_para_filtro(registro["titulo"] + " " + registro["extracto"] + " " + enlace)
            if palabras and not any(re.search(r"(?<!\w)" + re.escape(palabra) + r"(?!\w)", contenido)
                                    for palabra in palabras):
                continue
            entrada = entrada_desde_registro(enlace, registro, articulos, bool(miembro.get("texto_completo")))
            entrada["titulo"] = f"[{etiqueta}] {entrada['titulo']}"
            entrada["momento"] = momento_de_orden(entrada["fecha"], registro["visto"])
            entradas.append(entrada)
    por_enlace = {}
    for entrada in sorted(entradas, key=lambda e: e["momento"], reverse=True):
        por_enlace.setdefault(entrada["enlace"], entrada)
    ordenadas = sorted(por_enlace.values(), key=lambda e: e["momento"], reverse=True)
    return ordenadas[:MAXIMO_ENTRADAS_EN_FEED], avisos


def procesar_combinado(combinado, sitios_por_id, historial, articulos, raiz, descargas):
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    estado = {"id": combinado["id"], "nombre": combinado["nombre"], "url": "", "estado": "error", "detalle": "",
              "feed_propio": "", "entradas": 0, "combina": combinado["combina"], "actualizado": ahora}
    if combinado.get("version_url"):
        estado["version_url"] = combinado["version_url"]  # la página añade «?v=N» a la dirección del feed
    try:
        entradas, avisos = construir_combinado(combinado, sitios_por_id, historial, articulos)
        if combinado.get("texto_completo", True):
            completar_con_texto(entradas, articulos, descargas)
        else:
            completar_extractos(entradas, articulos, descargas)
        if len(avisos) == len(combinado["combina"]):
            estado["detalle"] = "ninguna de las webs tiene entradas: " + "; ".join(avisos)
            return estado
        repositorio = os.environ.get("GITHUB_REPOSITORY", "")
        enlace_del_canal = f"https://github.com/{repositorio}" if repositorio else "https://github.com"
        ruta_feed = os.path.join(raiz, "docs", "feeds", combinado["id"] + ".xml")
        escribir_texto(ruta_feed, extractor.construir_rss(combinado["nombre"], enlace_del_canal, entradas))
        estado["estado"] = "ok"
        estado["entradas"] = len(entradas)
        estado["con_texto_completo"] = sum(1 for entrada in entradas if entrada.get("cuerpo"))
        partes = [f"combina {len(combinado['combina'])} webs"]
        if combinado.get("filtro"):
            partes.append("filtro: " + ", ".join(combinado["filtro"]))
        estado["detalle"] = "; ".join(partes + avisos)
        return estado
    except Exception as error:
        estado["detalle"] = f"error interno: {type(error).__name__}: {error}"
        return estado


MINIMO_ENTRADAS_DEL_RESPALDO = 5
FRACCION_MINIMA_CON_FECHA_EN_RESPALDO = 0.5


def buscar_entradas_por_tarjetas(sitio, respuesta, decisiones, cliente, presupuesto):
    """Método principal: elige un bloque de tarjetas repetidas y filtra lo que no son artículos.

    Devuelve (entradas, motivo_del_fallo); sin entradas, el motivo explica por qué."""
    candidatos = extractor.detectar_candidatos(respuesta.texto, respuesta.url_final, MAXIMO_CANDIDATOS)
    if not candidatos:
        return [], "sin listado de artículos reconocible (¿página con JavaScript?)"
    decisiones_del_sitio = decisiones.setdefault(sitio["id"], {})
    elegido, motivo = elegir_grupo(sitio, candidatos, decisiones_del_sitio, cliente, presupuesto)
    if elegido is None:
        return [], motivo
    if cliente is not None:
        decisiones_del_sitio["firma"] = firma_sin_cantidad(elegido["firma"])
    entradas = filtrar_entradas(sitio, elegido["entradas"][:MAXIMO_ENTRADAS], decisiones_del_sitio, cliente, presupuesto)
    if not entradas:
        return [], "tras filtrar no queda ninguna entrada"
    return entradas, ""


def procesar_sitio(sitio, raiz, decisiones, cliente, presupuesto, articulos, descargas, historial):
    """Devuelve el estado público del sitio. Nunca lanza: un fallo no debe parar a los demás."""
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    estado = {"id": sitio["id"], "nombre": sitio["nombre"], "url": sitio["url"], "estado": "error",
              "detalle": "", "feed_propio": "", "entradas": 0, "user_agent": "feedmaker", "actualizado": ahora}
    if sitio.get("version_url"):
        estado["version_url"] = sitio["version_url"]  # la página añade «?v=N» a la dirección del feed generado
    if sitio.get("oculto"):
        estado["oculto"] = True  # solo es ingrediente de un combinado: la página no la enseña en la lista
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

        feed_propio, entradas_del_feed = extractor.descubrir_feed(respuesta.texto, respuesta.url_final)
        # Un feed enlazado desde la página no siempre sirve: el de toda la sección Deportes no es el de la
        # página de un solo equipo, y un feed sin entradas no sirve para nada. En esos casos se intenta
        # generar el feed de la página y, si no sale, se conserva el feed propio.
        feed_generico = bool(feed_propio) and (
            entradas_del_feed == 0
            or not extractor.feed_corresponde_a_la_pagina(respuesta.texto, respuesta.url_final, feed_propio))
        entradas = None
        if feed_propio and not feed_generico and not sitio.get("forzar_tarjetas"):
            if sitio.get("leer_feed_propio"):
                # Para webs que alimentan un combinado: se leen las entradas del feed, que traen fechas reales.
                respuesta_feed = extractor.peticion(feed_propio)
                if 200 <= respuesta_feed.estado < 300:
                    entradas = extractor.entradas_de_feed(respuesta_feed.texto)[:MAXIMO_ENTRADAS] or None
            if entradas is None:
                return estado_con_feed_propio(estado, feed_propio, "la web ya tiene feed: suscríbete a ese")

        if entradas is None and sitio.get("modo_enlaces"):
            # Sin IA: los enlaces de una página repartida en bloques distintos no forman un grupo de tarjetas.
            entradas = extractor.entradas_por_enlaces(
                respuesta.texto, respuesta.url_final, sitio.get("zona_horaria_de_las_urls"))[:MAXIMO_ENTRADAS_POR_ENLACES]
            if not entradas:
                estado["detalle"] = "no se han encontrado enlaces que parezcan artículos"
                return estado

        usa_respaldo_por_enlaces = False
        if entradas is None:
            entradas, motivo_del_fallo = buscar_entradas_por_tarjetas(sitio, respuesta, decisiones, cliente, presupuesto)
            if not entradas:
                # Las tarjetas no han dado nada utilizable: se prueba con los enlaces que parecen artículos.
                respaldo = extractor.entradas_por_enlaces(
                    respuesta.texto, respuesta.url_final, sitio.get("zona_horaria_de_las_urls"))[:MAXIMO_ENTRADAS_POR_ENLACES]
                if len(respaldo) >= MINIMO_ENTRADAS_DEL_RESPALDO:
                    entradas = respaldo
                    usa_respaldo_por_enlaces = True
                elif feed_generico:
                    return estado_con_feed_propio(estado, feed_propio, FEED_GENERICO_SIN_ALTERNATIVA)
                else:
                    estado["detalle"] = motivo_del_fallo
                    return estado

        con_texto = 0
        if quiere_texto_completo(sitio):
            con_texto = completar_con_texto(entradas, articulos, descargas)
        else:
            completar_fechas(entradas, articulos, descargas, decisiones.setdefault(sitio["id"], {}))

        if not sitio.get("oculto"):
            completar_extractos(entradas, articulos, descargas)

        if usa_respaldo_por_enlaces:
            # Los enlaces largos también son fichas de producto o menús: solo los artículos acaban con fecha.
            if sum(1 for entrada in entradas if entrada["fecha"]) < FRACCION_MINIMA_CON_FECHA_EN_RESPALDO * len(entradas):
                if feed_generico:
                    return estado_con_feed_propio(estado, feed_propio, FEED_GENERICO_SIN_ALTERNATIVA)
                estado["detalle"] = motivo_del_fallo + " (el respaldo por enlaces no encuentra fechas: no parecen artículos)"
                return estado

        entradas_del_feed = fusionar_con_historial(
            historial.setdefault(sitio["id"], {}), entradas, sitio.get("conservar_dias", CONSERVAR_DIAS_POR_DEFECTO),
            datetime.now(timezone.utc), articulos, quiere_texto_completo(sitio))
        con_texto = sum(1 for entrada in entradas_del_feed if entrada.get("cuerpo"))

        ruta_feed = os.path.join(raiz, "docs", "feeds", sitio["id"] + ".xml")
        escribir_texto(ruta_feed, extractor.construir_rss(sitio["nombre"], respuesta.url_final, entradas_del_feed))
        estado["estado"] = "ok"
        estado["entradas"] = len(entradas_del_feed)
        estado["con_texto_completo"] = con_texto
        estado["detalle"] = "con IA" if cliente is not None else "sin IA (no hay clave de API)"
        if usa_respaldo_por_enlaces:
            estado["detalle"] += "; listado por enlaces (las tarjetas no daban nada)"
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
    ruta_historial = os.path.join(raiz, "datos", "historial.json")
    historial = leer_json(ruta_historial, {})
    descargas = {"hechas": 0, "tope": int(os.environ.get("FEEDMAKER_MAX_ARTICULOS", "60"))}
    estado_anterior = {entrada["id"]: entrada for entrada in leer_json(ruta_estado, {}).get("sitios", [])}

    cliente = ia.crear_cliente()
    presupuesto = Presupuesto(TOPE_LLAMADAS_IA)
    print("IA:", "activada (Haiku 5.5)" if cliente else "desactivada: no hay ANTHROPIC_API_KEY", flush=True)

    resultados = []
    for sitio in [sitio for sitio in sitios if "combina" not in sitio]:
        estado = procesar_sitio(sitio, raiz, decisiones, cliente, presupuesto, articulos, descargas, historial)
        anterior = estado_anterior.get(sitio["id"])
        if estado["estado"] == "error" and anterior and anterior["estado"] == "ok":
            # El feed anterior sigue publicado: se conserva su número de entradas y se anota el fallo.
            estado["entradas"] = anterior["entradas"]
            estado["detalle"] = "no se pudo actualizar (se mantiene el feed anterior): " + estado["detalle"]
        resultados.append(estado)
        print(f"- {sitio['id']}: {estado['estado']} ({estado['entradas']} entradas) {estado['detalle']}", flush=True)

    sitios_por_id = {sitio["id"]: sitio for sitio in sitios}
    for combinado in [sitio for sitio in sitios if "combina" in sitio]:
        estado = procesar_combinado(combinado, sitios_por_id, historial, articulos, raiz, descargas)
        resultados.append(estado)
        print(f"- {combinado['id']}: {estado['estado']} ({estado['entradas']} entradas) {estado['detalle']}", flush=True)

    escribir_json(ruta_decisiones, decisiones)
    escribir_json(ruta_historial, historial)
    if articulos:
        # La caché de artículos solo guarda lo que sigue en algún historial: así caduca junto con él.
        enlaces_en_historial = {enlace for del_sitio in historial.values() for enlace in del_sitio}
        escribir_json(ruta_articulos, {enlace: dato for enlace, dato in articulos.items() if enlace in enlaces_en_historial})
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
