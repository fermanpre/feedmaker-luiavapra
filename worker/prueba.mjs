// Pruebas del Worker sin red ni cuenta de Cloudflare: simulan `fetch` (GitHub) y el KV.
// Uso: node worker/prueba.mjs
import servicio, { validarUrl } from "./src/index.js";

const ORIGEN = "https://fermanpre.github.io";
let fallos = 0;
function comprobar(nombre, condicion, detalle = "") {
  console.log(`${condicion ? "OK   " : "FALLO"} ${nombre} ${condicion ? "" : detalle}`);
  if (!condicion) fallos += 1;
}

function crearKv() {
  const datos = new Map();
  return { get: async (k) => datos.get(k) ?? null, put: async (k, v) => { datos.set(k, v); } };
}

let respuestaGithub = 204;
let llamadasGithub = [];
globalThis.fetch = async (url, opciones) => {
  llamadasGithub.push({ url, opciones });
  return new Response(null, { status: respuestaGithub });
};

function entorno(extra = {}) {
  return { REPO: "fermanpre/feedmaker", ORIGEN_PERMITIDO: ORIGEN, GITHUB_TOKEN: "token-de-prueba",
           CLAVE: "secreta", MAX_ALTAS_DIA: "2", MAX_FALLOS_HORA: "3", LIMITES: crearKv(), ...extra };
}

function peticion(cuerpo, { origen = ORIGEN, metodo = "POST", ip = "1.2.3.4" } = {}) {
  return new Request("https://feedmaker.ejemplo.workers.dev/", {
    method: metodo,
    headers: { Origin: origen, "Content-Type": "application/json", "CF-Connecting-IP": ip },
    body: metodo === "POST" ? JSON.stringify(cuerpo) : undefined,
  });
}

async function llamar(env, cuerpo, opciones) {
  const respuesta = await servicio.fetch(peticion(cuerpo, opciones), env);
  return { estado: respuesta.status, datos: respuesta.status === 204 ? null : await respuesta.json() };
}

// Validación de URL
comprobar("URL https válida", validarUrl("https://www.ejemplo.com/blog/") === "");
comprobar("rechaza http", validarUrl("http://ejemplo.com") !== "");
comprobar("rechaza IP", validarUrl("https://127.0.0.1/") !== "");
comprobar("rechaza localhost", validarUrl("https://localhost/") !== "");
comprobar("rechaza puerto", validarUrl("https://ejemplo.com:8443/") !== "");
comprobar("rechaza credenciales", validarUrl("https://u:p@ejemplo.com/") !== "");
comprobar("rechaza host sin punto", validarUrl("https://intranet/") !== "");
comprobar("rechaza texto vacío", validarUrl("") !== "");
comprobar("rechaza URL enorme", validarUrl("https://ejemplo.com/" + "a".repeat(2100)) !== "");

// Flujo normal
let env = entorno();
let r = await llamar(env, { url: "https://www.ejemplo.com/blog/", clave: "secreta" });
comprobar("alta correcta devuelve 200", r.estado === 200 && r.datos.ok === true, JSON.stringify(r));
const llamada = llamadasGithub[0];
comprobar("llama al endpoint correcto", llamada.url === "https://api.github.com/repos/fermanpre/feedmaker/actions/workflows/alta.yml/dispatches");
comprobar("envía el token como Bearer", llamada.opciones.headers.Authorization === "Bearer token-de-prueba");
comprobar("envía la URL como input", JSON.parse(llamada.opciones.body).inputs.url === "https://www.ejemplo.com/blog/");
comprobar("envía User-Agent (GitHub lo exige)", Boolean(llamada.opciones.headers["User-Agent"]));

// Texto completo: solo el booleano true lo activa
comprobar("por defecto envía texto_completo=false", JSON.parse(llamada.opciones.body).inputs.texto_completo === "false");
llamadasGithub = [];
await llamar(entorno(), { url: "https://www.ejemplo.com/", clave: "secreta", texto_completo: true });
comprobar("texto_completo true se reenvía como «true»", JSON.parse(llamadasGithub[0].opciones.body).inputs.texto_completo === "true");
llamadasGithub = [];
await llamar(entorno(), { url: "https://www.ejemplo.com/", clave: "secreta", texto_completo: "true" });
comprobar("la cadena «true» no cuenta: solo el booleano", JSON.parse(llamadasGithub[0].opciones.body).inputs.texto_completo === "false");

// Seguridad
llamadasGithub = [];
r = await llamar(env, { url: "https://www.ejemplo.com/", clave: "mala" });
comprobar("contraseña incorrecta devuelve 401", r.estado === 401);
comprobar("con contraseña incorrecta no se llama a GitHub", llamadasGithub.length === 0);
r = await llamar(env, { url: "https://www.ejemplo.com/", clave: "secreta" }, { origen: "https://malo.com" });
comprobar("origen distinto devuelve 403", r.estado === 403);
r = await servicio.fetch(peticion(null, { metodo: "GET" }), env);
comprobar("método GET devuelve 405", r.status === 405);
r = await servicio.fetch(peticion(null, { metodo: "OPTIONS" }), env);
comprobar("OPTIONS devuelve 204 con CORS", r.status === 204 && r.headers.get("Access-Control-Allow-Origin") === ORIGEN);
r = await llamar(env, { url: "http://inseguro.com", clave: "secreta" });
comprobar("URL inválida devuelve 400 sin llamar a GitHub", r.estado === 400 && llamadasGithub.length === 0);
r = await llamar(env, { url: "https://www.ejemplo.com/", clave: undefined });
comprobar("sin contraseña devuelve 401", r.estado === 401);

// Límite de intentos fallidos (MAX_FALLOS_HORA = 3)
env = entorno();
for (let i = 0; i < 3; i += 1) await llamar(env, { url: "https://a.com/", clave: "mala" }, { ip: "9.9.9.9" });
r = await llamar(env, { url: "https://a.com/", clave: "secreta" }, { ip: "9.9.9.9" });
comprobar("tras 3 fallos bloquea la IP (429), aun con la clave buena", r.estado === 429);
r = await llamar(env, { url: "https://a.com/", clave: "secreta" }, { ip: "8.8.8.8" });
comprobar("otra IP no está bloqueada", r.estado === 200);

// Tope de altas por día (MAX_ALTAS_DIA = 2)
env = entorno();
await llamar(env, { url: "https://a.com/", clave: "secreta" });
await llamar(env, { url: "https://b.com/", clave: "secreta" });
r = await llamar(env, { url: "https://c.com/", clave: "secreta" });
comprobar("el tercer alta del día devuelve 429", r.estado === 429);

// Error de GitHub
env = entorno();
respuestaGithub = 404;
r = await llamar(env, { url: "https://a.com/", clave: "secreta" });
comprobar("error de GitHub devuelve 502 sin filtrar detalles", r.estado === 502 && !JSON.stringify(r.datos).includes("token"));
respuestaGithub = 204;

// Sin KV (opcional): funciona sin límites
env = entorno({ LIMITES: undefined });
r = await llamar(env, { url: "https://a.com/", clave: "secreta" });
comprobar("sin KV funciona igualmente", r.estado === 200);

console.log(fallos === 0 ? "\nTodas las pruebas pasan." : `\n${fallos} prueba(s) fallan.`);
process.exit(fallos === 0 ? 0 : 1);
