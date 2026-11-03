// Pruebas del Worker para peticiones de combinado, sin red ni cuenta de Cloudflare.
// Uso: node worker/prueba_combinado.mjs
import servicio, { prepararCombinado } from "./src/index.js";

const ORIGEN = "https://fermanpre.github.io";
const SALTO = String.fromCharCode(10);
let fallos = 0;
function comprobar(nombre, condicion, detalle = "") {
  console.log(`${condicion ? "OK   " : "FALLO"} ${nombre} ${condicion ? "" : detalle}`);
  if (!condicion) fallos += 1;
}

function crearKv() {
  const datos = new Map();
  return { get: async (k) => datos.get(k) ?? null, put: async (k, v) => { datos.set(k, v); } };
}

let llamadasGithub = [];
globalThis.fetch = async (url, opciones) => {
  llamadasGithub.push({ url, opciones });
  return new Response(null, { status: 204 });
};

function entorno(extra = {}) {
  return { REPO: "fermanpre/feedmaker", ORIGEN_PERMITIDO: ORIGEN, GITHUB_TOKEN: "token-de-prueba",
           CLAVE: "secreta", MAX_ALTAS_DIA: "10", MAX_FALLOS_HORA: "3", LIMITES: crearKv(), ...extra };
}

async function llamar(env, cuerpo) {
  const peticion = new Request("https://feedmaker.ejemplo.workers.dev/", {
    method: "POST",
    headers: { Origin: ORIGEN, "Content-Type": "application/json", "CF-Connecting-IP": "1.2.3.4" },
    body: JSON.stringify(cuerpo),
  });
  const respuesta = await servicio.fetch(peticion, env);
  return { estado: respuesta.status, datos: await respuesta.json() };
}

function entradasEnviadas() {
  return JSON.parse(llamadasGithub[llamadasGithub.length - 1].opciones.body).inputs;
}

// Validación directa
comprobar("combinado válido", prepararCombinado({ combina: ["a", "b"] }).inputs?.combina === "a,b");
comprobar("rechaza un solo feed", prepararCombinado({ combina: ["a"] }).motivo !== undefined);
comprobar("rechaza seis feeds", prepararCombinado({ combina: ["a", "b", "c", "d", "e", "f"] }).motivo !== undefined);
comprobar("acepta cinco feeds", prepararCombinado({ combina: ["a", "b", "c", "d", "e"] }).inputs !== undefined);
comprobar("rechaza repetidos", prepararCombinado({ combina: ["a", "a"] }).motivo === "hay feeds repetidos");
comprobar("rechaza combina que no es lista", prepararCombinado({ combina: "a,b" }).motivo !== undefined);
for (const hostil of ["../x", "a;b", "A", "a b", "-a", "", "a" + SALTO + "b", 5, null, { a: 1 }]) {
  comprobar(`rechaza el identificador ${JSON.stringify(hostil)}`, prepararCombinado({ combina: ["ok", hostil] }).motivo !== undefined);
}
comprobar("rechaza un identificador de más de 80 caracteres", prepararCombinado({ combina: ["a", "a".repeat(81)] }).motivo !== undefined);
comprobar("rechaza más de 5 palabras de filtro", prepararCombinado({ combina: ["a", "b"], filtro: ["1", "2", "3", "4", "5", "6"] }).motivo !== undefined);
comprobar("rechaza una palabra de más de 40 caracteres", prepararCombinado({ combina: ["a", "b"], filtro: ["x".repeat(41)] }).motivo !== undefined);
comprobar("rechaza una palabra con coma", prepararCombinado({ combina: ["a", "b"], filtro: ["uno,dos"] }).motivo !== undefined);
comprobar("rechaza una palabra vacía", prepararCombinado({ combina: ["a", "b"], filtro: ["  "] }).motivo !== undefined);
comprobar("rechaza una palabra con salto de línea", prepararCombinado({ combina: ["a", "b"], filtro: ["a" + SALTO + "b"] }).motivo !== undefined);
comprobar("rechaza un filtro que no es lista", prepararCombinado({ combina: ["a", "b"], filtro: "caja" }).motivo !== undefined);
comprobar("rechaza un nombre que no es texto", prepararCombinado({ combina: ["a", "b"], nombre: 7 }).motivo !== undefined);
comprobar("rechaza un nombre larguísimo", prepararCombinado({ combina: ["a", "b"], nombre: "x".repeat(201) }).motivo !== undefined);
const limpio = prepararCombinado({ combina: ["a", "b"], nombre: "Mi" + SALTO + "nombre ", filtro: [" caja 87 ", "Caja87"] }).inputs;
comprobar("limpia el nombre y recorta las palabras", limpio.nombre_combinado === "Mi nombre" && limpio.filtro_combinado === "caja 87,Caja87", JSON.stringify(limpio));

// A través del servicio
let env = entorno();
let r = await llamar(env, { clave: "secreta", combina: ["a", "b", "c"], nombre: "Mi combinado", filtro: ["caja 87"], texto_completo: true });
comprobar("petición de combinado válida: 200", r.estado === 200 && r.datos.ok === true, JSON.stringify(r));
let enviados = entradasEnviadas();
comprobar("reenvía combina, nombre y filtro al workflow", enviados.combina === "a,b,c" && enviados.nombre_combinado === "Mi combinado" && enviados.filtro_combinado === "caja 87", JSON.stringify(enviados));
comprobar("la url va vacía y texto_completo true", enviados.url === "" && enviados.texto_completo === "true");
comprobar("llama al mismo workflow alta.yml", llamadasGithub[llamadasGithub.length - 1].url.endsWith("/actions/workflows/alta.yml/dispatches"));

llamadasGithub = [];
r = await llamar(env, { clave: "secreta", combina: ["a", "b"], texto_completo: "true" });
comprobar("la cadena «true» no activa el texto completo (solo el booleano)", entradasEnviadas().texto_completo === "false");

llamadasGithub = [];
r = await llamar(env, { clave: "secreta", url: "https://web.com/", combina: ["a", "b"] });
comprobar("con «combina» se ignora «url»", entradasEnviadas().url === "" && entradasEnviadas().combina === "a,b");

llamadasGithub = [];
r = await llamar(env, { clave: "mala", combina: ["a", "b"] });
comprobar("contraseña incorrecta: 401 y no se llama a GitHub", r.estado === 401 && llamadasGithub.length === 0);
r = await llamar(env, { clave: "secreta", combina: ["a"] });
comprobar("un combinado inválido devuelve 400 sin llamar a GitHub", r.estado === 400 && llamadasGithub.length === 0, JSON.stringify(r));
comprobar("   y el mensaje lo explica", r.datos.mensaje.includes("2 a 5"), r.datos.mensaje);

// El tope diario cuenta también los combinados
env = entorno({ MAX_ALTAS_DIA: "2" });
await llamar(env, { clave: "secreta", combina: ["a", "b"] });
await llamar(env, { clave: "secreta", url: "https://web.com/" });
r = await llamar(env, { clave: "secreta", combina: ["a", "c"] });
comprobar("el tope de altas por día cuenta webs y combinados juntos", r.estado === 429, JSON.stringify(r));

// El alta de una web sigue como siempre
llamadasGithub = [];
env = entorno();
r = await llamar(env, { clave: "secreta", url: "https://web.com/blog", texto_completo: true });
const web = entradasEnviadas();
comprobar("el alta de una web no cambia", r.estado === 200 && web.url === "https://web.com/blog" && web.texto_completo === "true" && web.combina === undefined, JSON.stringify(web));

console.log(fallos === 0 ? "\nTodas las pruebas pasan." : `\n${fallos} prueba(s) fallan.`);
process.exit(fallos === 0 ? 0 : 1);
