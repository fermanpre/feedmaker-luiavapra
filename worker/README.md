# Servicio intermedio (Cloudflare Worker)

Permite que otra persona dé de alta webs desde la página con una **contraseña**, sin tener cuenta de GitHub ni
tu token. El token de GitHub vive solo en Cloudflare, como secreto.

    página (GitHub Pages) --URL + contraseña--> Worker --workflow_dispatch--> GitHub Actions

Lo que hace el Worker: comprueba el origen, la contraseña (en tiempo constante) y la URL (https, sin IP ni puerto),
aplica los topes y lanza `alta.yml`. No guarda ningún dato de quien lo usa.

## Topes

| Variable (`wrangler.toml`) | Por defecto | Efecto |
|---|---|---|
| `MAX_ALTAS_DIA` | 30 | Altas por día entre todos los usuarios. |
| `MAX_FALLOS_HORA` | 5 | Contraseñas incorrectas por IP y hora; después, bloqueo de una hora. |

Ambos topes necesitan el espacio KV `LIMITES` (paso 5). Sin él, el Worker funciona pero **sin límites**; la
contraseña es entonces la única barrera. Cada alta consume llamadas a la API de pago, con un máximo de 60 por ejecución.

## Puesta en marcha

Los nombres de los comandos están escritos de memoria: si alguno falla, mira `npx wrangler --help`.

1. Cuenta gratuita en Cloudflare (https://dash.cloudflare.com). En Workers & Pages, Cloudflare te pedirá elegir un
   subdominio `<nombre>.workers.dev`.
2. Desde esta carpeta: `npx wrangler login` (abre el navegador para autorizar).
3. **Token de GitHub** nuevo, solo para el Worker (no reutilices el de tu navegador): Settings → Developer settings →
   Fine-grained tokens, limitado al repositorio `feedmaker`, con **Actions: lectura y escritura** y nada más.
4. Guardar los secretos. Cada comando te pide el valor por teclado; no lo escribas en el comando ni lo pegues en un chat:

       npx wrangler secret put GITHUB_TOKEN
       npx wrangler secret put CLAVE

   `CLAVE` es la contraseña que le darás a quien quieras. Usa una larga y que no uses en otro sitio.
5. (Recomendado) Crear el espacio de límites y enlazarlo:

       npx wrangler kv namespace create LIMITES

   Copia el `id` que devuelva en `wrangler.toml`, descomentando el bloque `[[kv_namespaces]]`.
6. Desplegar: `npx wrangler deploy`. Devuelve la dirección `https://feedmaker.<nombre>.workers.dev`.
7. En `docs/index.html`, pon esa dirección en `var URL_SERVICIO = "…";` y sube el cambio. Entonces la página muestra
   el campo de contraseña.

## Probarlo

- Sin red ni cuenta, con GitHub y KV simulados: `node worker/prueba.mjs`.
- Con el servicio desplegado, desde la página: pega la contraseña, una URL nueva y «Obtener enlace».

## Cosas a tener en cuenta

- **Cualquiera con la contraseña** puede dar de alta webs hasta el tope diario. Cámbiala con `wrangler secret put CLAVE`
  cuando quieras; la página de cada persona dejará de funcionar hasta que la vuelva a pegar.
- **Las webs dadas de alta son públicas**: `estado.json` y los feeds están en un repositorio público.
- Para retirar a alguien, cambia la contraseña. El token de GitHub no se ha compartido con nadie.
- Para pararlo del todo: `npx wrangler delete`, o revoca el token de GitHub.
