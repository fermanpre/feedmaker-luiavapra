// Servicio intermedio de Feedmaker (Cloudflare Worker).
//
// La página envía la URL y una contraseña; el Worker, que guarda el token de GitHub como secreto,
// lanza el workflow «alta.yml». Así el token nunca llega al navegador y se puede dar acceso a otras
// personas con una contraseña que se cambia sin tocar nada más.
//
// Secretos (wrangler secret put): GITHUB_TOKEN (fine-grained, solo Actions: lectura y escritura) y CLAVE.
// Variables (wrangler.toml): REPO, ORIGEN_PERMITIDO, MAX_ALTAS_DIA, MAX_FALLOS_HORA.
// KV opcional (LIMITES): si existe, limita los intentos fallidos por IP y las altas por día.

const ENCABEZADOS_JSON = { "Content-Type": "application/json; charset=utf-8" };
const LONGITUD_MAXIMA_URL = 2000;

function cabecerasCors(env) {
  return {
    "Access-Control-Allow-Origin": env.ORIGEN_PERMITIDO,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

function responder(env, estado, cuerpo) {
  return new Response(JSON.stringify(cuerpo), {
    status: estado,
    headers: { ...ENCABEZADOS_JSON, ...cabecerasCors(env) },
  });
}

// Misma validación que extractor.validar_url (Python): el workflow la repite, esto evita lanzarlo en balde.
export function validarUrl(texto) {
  if (typeof texto !== "string" || texto.length === 0 || texto.length > LONGITUD_MAXIMA_URL) {
    return "URL no válida";
  }
  let direccion;
  try {
    direccion = new URL(texto);
  } catch (error) {
    return "URL no válida";
  }
  if (direccion.protocol !== "https:") return "solo se admiten URLs https";
  const host = direccion.hostname;
  if (!host || !host.includes(".") || host === "localhost") return "host no válido";
  if (/^[\d.]+$/.test(host) || host.startsWith("[")) return "no se admiten direcciones IP";
  if (direccion.port !== "") return "no se admiten puertos distintos de 443";
  if (direccion.username || direccion.password) return "no se admiten credenciales en la URL";
  return "";
}

// Comparación en tiempo constante: se comparan los hashes SHA-256 de las dos cadenas.
async function clavesIguales(recibida, esperada) {
  const codificador = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", codificador.encode(String(recibida))),
    crypto.subtle.digest("SHA-256", codificador.encode(String(esperada))),
  ]);
  const va = new Uint8Array(a);
  const vb = new Uint8Array(b);
  let diferencia = 0;
  for (let i = 0; i < va.length; i += 1) diferencia |= va[i] ^ vb[i];
  return diferencia === 0;
}

async function leerContador(env, clave) {
  if (!env.LIMITES) return 0;
  return parseInt((await env.LIMITES.get(clave)) || "0", 10);
}

async function sumarContador(env, clave, segundosDeVida) {
  if (!env.LIMITES) return;
  const actual = await leerContador(env, clave);
  await env.LIMITES.put(clave, String(actual + 1), { expirationTtl: segundosDeVida });
}

const PATRON_DE_IDENTIFICADOR = /^[a-z0-9][a-z0-9-]{0,79}$/;
const PATRON_DE_CONTROL = /[\u0000-\u001f\u007f]/;
const MINIMO_DE_FEEDS = 2;
const MAXIMO_DE_FEEDS = 5;
const MAXIMO_DE_PALABRAS_DE_FILTRO = 5;
const MAXIMA_LONGITUD_DE_PALABRA = 40;
const MAXIMA_LONGITUD_DEL_NOMBRE = 200;

// Valida una petición de combinado ({combina, nombre?, filtro?}). Devuelve { motivo } si no vale o { inputs } con los
// parámetros que espera alta.yml. alta.py vuelve a validarlo todo: esto evita lanzar un workflow en balde.
export function prepararCombinado(datos) {
  const identificadores = datos.combina;
  if (!Array.isArray(identificadores) || identificadores.length < MINIMO_DE_FEEDS || identificadores.length > MAXIMO_DE_FEEDS) {
    return { motivo: `un combinado lleva de ${MINIMO_DE_FEEDS} a ${MAXIMO_DE_FEEDS} feeds` };
  }
  if (!identificadores.every((i) => typeof i === "string" && PATRON_DE_IDENTIFICADOR.test(i))) {
    return { motivo: "identificador de feed no válido" };
  }
  if (new Set(identificadores).size !== identificadores.length) {
    return { motivo: "hay feeds repetidos" };
  }
  const nombre = datos.nombre === undefined || datos.nombre === null ? "" : datos.nombre;
  if (typeof nombre !== "string" || nombre.length > MAXIMA_LONGITUD_DEL_NOMBRE) {
    return { motivo: "nombre no válido" };
  }
  const filtro = datos.filtro === undefined || datos.filtro === null ? [] : datos.filtro;
  if (!Array.isArray(filtro) || filtro.length > MAXIMO_DE_PALABRAS_DE_FILTRO) {
    return { motivo: `el filtro admite hasta ${MAXIMO_DE_PALABRAS_DE_FILTRO} palabras` };
  }
  const palabrasValidas = filtro.every((p) => typeof p === "string" && p.trim().length > 0 &&
    p.length <= MAXIMA_LONGITUD_DE_PALABRA && !p.includes(",") && !PATRON_DE_CONTROL.test(p));
  if (!palabrasValidas) {
    return { motivo: "palabra de filtro no válida" };
  }
  return {
    inputs: {
      url: "",
      combina: identificadores.join(","),
      nombre_combinado: nombre.replace(PATRON_DE_CONTROL, " ").trim(),
      filtro_combinado: filtro.map((p) => p.trim()).join(","),
      texto_completo: datos.texto_completo === true ? "true" : "false",
    },
  };
}

async function lanzarWorkflow(env, inputs) {
  const respuesta = await fetch(
    `https://api.github.com/repos/${env.REPO}/actions/workflows/alta.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "feedmaker-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    }
  );
  return respuesta.status;
}

export async function manejar(peticion, env) {
  if (peticion.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: cabecerasCors(env) });
  }
  if (peticion.method !== "POST") {
    return responder(env, 405, { ok: false, mensaje: "Método no permitido." });
  }
  const origen = peticion.headers.get("Origin");
  if (origen !== env.ORIGEN_PERMITIDO) {
    return responder(env, 403, { ok: false, mensaje: "Origen no permitido." });
  }

  let datos;
  try {
    datos = await peticion.json();
  } catch (error) {
    return responder(env, 400, { ok: false, mensaje: "Petición mal formada." });
  }

  const ip = peticion.headers.get("CF-Connecting-IP") || "desconocida";
  const hora = new Date().toISOString().slice(0, 13);
  const dia = new Date().toISOString().slice(0, 10);
  const maximoFallos = parseInt(env.MAX_FALLOS_HORA || "5", 10);
  const maximoAltas = parseInt(env.MAX_ALTAS_DIA || "30", 10);

  if ((await leerContador(env, `fallos:${ip}:${hora}`)) >= maximoFallos) {
    return responder(env, 429, { ok: false, mensaje: "Demasiados intentos fallidos. Prueba dentro de una hora." });
  }
  if (!(await clavesIguales(datos.clave ?? "", env.CLAVE))) {
    await sumarContador(env, `fallos:${ip}:${hora}`, 3600);
    return responder(env, 401, { ok: false, mensaje: "Contraseña incorrecta." });
  }

  // Una petición con «combina» es un combinado (y se ignora «url»); si no, es el alta de una web.
  let inputs;
  if (datos.combina !== undefined) {
    const combinado = prepararCombinado(datos);
    if (combinado.motivo) {
      return responder(env, 400, { ok: false, mensaje: combinado.motivo });
    }
    inputs = combinado.inputs;
  } else {
    const motivo = validarUrl(datos.url);
    if (motivo) {
      return responder(env, 400, { ok: false, mensaje: motivo });
    }
    // Solo el booleano true activa el texto completo; cualquier otro valor se trata como «no».
    inputs = { url: datos.url, texto_completo: datos.texto_completo === true ? "true" : "false" };
  }
  if ((await leerContador(env, `altas:${dia}`)) >= maximoAltas) {
    return responder(env, 429, { ok: false, mensaje: "Se ha alcanzado el máximo de altas de hoy." });
  }

  const estado = await lanzarWorkflow(env, inputs);
  if (estado !== 204) {
    // El detalle del error de GitHub no se devuelve a la página: podría revelar información del repositorio.
    return responder(env, 502, { ok: false, mensaje: `GitHub no ha aceptado el lanzamiento (código ${estado}).` });
  }
  await sumarContador(env, `altas:${dia}`, 172800);
  return responder(env, 200, { ok: true, mensaje: "Workflow lanzado." });
}

export default { fetch: manejar };
