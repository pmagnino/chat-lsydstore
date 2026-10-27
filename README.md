# Chatbot de LSyD Store

Asistente de chat con IA (Claude) para https://www.lsydstore.com.ar/.
Ayuda a los clientes a encontrar productos del catálogo real por lenguaje
natural ("busco una mochila para notebook") y les devuelve nombre, precio,
colores y link directo al producto.

Es el mismo proyecto que el chat de Lesedife (repo `lesedifechat`),
adaptado a LSyD Store: el sitio es de la misma agencia (Positive Media),
pero LSyD es minorista y **publica los precios**, así que el bot los da
(precio final con IVA, y si está en oferta, el precio de lista y el
descuento). Corre en el **puerto 5001** para no chocar con el de Lesedife
(5000) si están en el mismo servidor.

La solución se aloja aparte y se integra en lsydstore.com.ar con **una
sola línea de `<script>`** que le tenés que pasar a la agencia para que la
peguen antes de `</body>`.

## Cómo está armado

- `scraper.py`: lee las categorías y subcategorías del menú del sitio,
  recorre todas sus páginas y arma `products.json` con el catálogo (nombre,
  código, categoría, precio, colores, imagen, link, medidas y peso).
- `catalog.py`: búsqueda en memoria sobre `products.json` por nombre,
  categoría, código o color. Tolera plurales ("mochilas") y acentos ("rinonera").
- `app.py`: backend Flask. Expone `POST /api/chat`: recibe el mensaje del
  cliente, deja que Claude busque productos con la tool `search_products`
  y responde en español con recomendaciones reales (nunca inventa
  productos ni precios: los toma del catálogo). Tiene un límite de consultas por IP.
- `widget/lsydstore-chatbot.js`: el widget embebible (burbuja de chat
  flotante), en JS puro, sin dependencias. Se sirve desde el propio
  backend en `/widget/lsydstore-chatbot.js`.
- `test.html`: página para probar el widget en tu máquina.

## 1. Configurar

```bash
python -m venv venv
source venv/Scripts/activate   # Windows (Git Bash). En cmd: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Editá .env y poné tu ANTHROPIC_API_KEY real (la conseguís en console.anthropic.com)
```

## 2. Catálogo

El bot **no entra a lsydstore.com.ar en cada consulta**: responde desde
`products.json`, que tiene cargado en memoria. Ese archivo se actualiza
solo: **cada 8 horas** el backend vuelve a scrapear el sitio en segundo
plano (tarda unos minutos) y empieza a usar la lista nueva sin reiniciarse.
Para cambiar la frecuencia, en `.env`:

```
CATALOG_REFRESH_HOURS=8   # 0 = desactivado
```

Como los precios salen de ese archivo, el bot puede tener un precio de
hasta 8 horas de antigüedad (le avisa al cliente que el que vale es el de
la web). Si cambian los precios seguido, bajá ese número (por ejemplo a 2).

Si un scrapeo falla, o trae menos de la mitad de los productos actuales
(señal de que el sitio cambió o estaba caído), se descarta, el bot sigue
con la lista anterior y se reintenta en 1 hora.

También podés actualizarlo a mano en cualquier momento, y el backend lo
toma solo en la siguiente búsqueda:

```bash
python scraper.py
```

## Preguntas frecuentes (pagos, mínimos, envíos...)

Lo que no está en el catálogo se carga a mano en `faq/faq.md` (ya viene
cargado con los Términos y Condiciones, el envío gratis, las cuotas y las
sucursales del sitio; la página de FAQ del sitio todavía es Lorem ipsum): cada
pregunta en una línea que empieza con `## ` y la respuesta abajo. El bot
responde esos temas **solo** con lo que diga ese archivo; si algo no está,
deriva al WhatsApp (no inventa). Las preguntas cuya respuesta dice
`COMPLETAR` se ignoran.

Con Docker, la carpeta `faq/` está montada desde el servidor: editás
`faq/faq.md` ahí mismo y el bot lo toma en la siguiente consulta, sin
rebuild ni reinicio.

## 3. Probar en local

El `.env` local ya viene listo para probar: sin Turnstile, sin proxy y con
la página de prueba prendida. Solo necesita `ANTHROPIC_API_KEY`.

```bash
python app.py
```

O con Docker (por ejemplo en un servidor Linux):

```bash
docker compose up -d --build
docker compose logs -f      # ver logs (Ctrl+C para salir)
```

Después abrí **`http://127.0.0.1:5001/test`** en el navegador (con
`ENABLE_TEST_PAGE=1`). La página de prueba la sirve el
propio backend. Probá el chat con preguntas tipo:

- "busco una mochila para la facultad"
- "qué valijas carry on tienen"
- "qué tienen de Stitch en oferta"
- "cuánto sale la mochila Wilson Urban" (tiene que dar el precio del catálogo)
- "hacen envíos? cuántas cuotas?" (responde desde `faq/faq.md`)

En producción podés apagar la página de prueba con `ENABLE_TEST_PAGE=0`.

## 4. Desplegar el backend

### Opción A: servidor propio con `chat.lsydstore.com.ar` (Cloudflare + HAProxy)

Cadena: cliente → **Cloudflare** (proxy naranja) → **HAProxy** (HTTPS) →
chatbot en el puerto 5001.

1. **Cloudflare:** registro **A** `chat` → IP pública de HAProxy, con el
   proxy activado (nube naranja). En SSL/TLS poné el modo **Full (strict)**
   (HAProxy necesita un certificado válido: sirve un *Origin Certificate*
   de Cloudflare). No uses "Flexible".
2. **HAProxy:** un backend que apunte al chatbot, con:
   ```
   backend lsydstore_chat
       option forwardfor
       timeout server 100s
       server chat1 IP-DEL-CHATBOT:5001 check
   ```
   `option forwardfor` es lo que hace llegar la IP real del cliente
   (`PROXY_HOPS=2`). El timeout largo es porque algunas respuestas tardan
   más de 30 segundos.
3. **Firewall:** que el 443 de HAProxy solo acepte las IPs de Cloudflare
   (https://www.cloudflare.com/ips/) y que el 5001 del chatbot solo acepte
   a HAProxy. Si HAProxy corre en otra máquina, poné `CHATBOT_BIND=0.0.0.0`
   en `.env` (por defecto el 5001 solo escucha en el propio servidor).
4. Copiá la carpeta al servidor y creá el `.env` a partir de `.env.example`
   (`ALLOWED_ORIGIN`, `PROXY_HOPS=2`, `ENABLE_TEST_PAGE=0`).
5. `docker compose up -d --build` (el compose tiene `name: lsydstore-chat`,
   así que no pisa los contenedores del chat de Lesedife aunque estén en el
   mismo servidor).
6. Probá `https://chat.lsydstore.com.ar/api/health` → `{"status": "ok"}`.
   Para usar la página de prueba, poné `ENABLE_TEST_PAGE=1`, hacé
   `docker compose up -d` y entrá a `https://chat.lsydstore.com.ar/test`
   (después volvé a apagarla).

### Opción B: Render, Railway o Fly.io

El servicio ya pone HTTPS adelante. Pasos generales:

1. Subí esta carpeta a un repositorio (GitHub).
2. Creá un servicio web nuevo apuntando al repo (o usá el `Dockerfile`).
3. Comando de arranque:
   `gunicorn --workers 2 --threads 4 --timeout 90 app:app`
4. Variables de entorno:
   - `ANTHROPIC_API_KEY`
   - `ALLOWED_ORIGIN=https://lsydstore.com.ar,https://www.lsydstore.com.ar`
   - `PROXY_HOPS=1` (estos servicios ponen un proxy adelante; sin esto el
     límite por IP no funciona bien)
5. Una vez desplegado vas a tener una URL fija, por ejemplo
   `https://lsydstore-chatbot.onrender.com`.

## 5. Embeber el widget en lsydstore.com.ar

Pasale este snippet a la agencia (Positive Media) para que lo agreguen
justo antes de `</body>` en el sitio, cambiando la URL por la real de tu
backend desplegado:

```html
<script src="https://chat.lsydstore.com.ar/widget/lsydstore-chatbot.js"
        data-api-url="https://chat.lsydstore.com.ar"
        data-turnstile-sitekey="TU-SITE-KEY-DE-TURNSTILE"></script>
```

No requiere nada más: el script inyecta su propio CSS y la burbuja de chat
flotante abajo a la derecha, en los colores de LSyD (negro y cobre). Si el sitio tiene
una Content-Security-Policy, tiene que permitir `https://challenges.cloudflare.com`
en `script-src` y `frame-src` (Turnstile), y `https://chat.lsydstore.com.ar` en
`script-src` y `connect-src`.

## Costos

Por defecto usa `claude-sonnet-5-5` con esfuerzo `low`: alcanza de sobra
para buscar y recomendar productos, responde en ~2-6 segundos y cuesta la
mitad que Opus. Si en algún momento querés probar con Opus, en `.env`:

```
ANTHROPIC_MODEL=claude-opus-5-5
```

Poné también un **tope de gasto mensual** en console.anthropic.com.

## Seguridad / a tener en cuenta

- **Cloudflare Turnstile** frena a los scripts que llamen a la API directo.
  Al abrir el chat, el widget pasa una verificación invisible y el backend
  le da una sesión de 30 minutos atada a su IP; `/api/chat` rechaza (401)
  las consultas sin sesión válida, y esas consultas no gastan los límites.
  Se activa con `TURNSTILE_SECRET_KEY` en `.env` (la secret no se comparte
  ni se sube a git) y `data-turnstile-sitekey` en el `<script>`. En el panel
  de Turnstile tienen que estar los dominios `lsydstore.com.ar` y, para usar
  `/test`, `chat.lsydstore.com.ar` (podés agregarlos al mismo widget de
  Lesedife o crear uno nuevo).
- El límite de consultas por IP (`CHAT_RATE_LIMIT`, por defecto 10 por
  minuto y 100 por día) es lo que protege tu cuota de la API. CORS
  (`ALLOWED_ORIGIN`) solo impide que otras páginas web usen el widget,
  pero no frena a alguien que llame a la API directo (con curl se puede
  poner cualquier `Origin`).
- `GLOBAL_RATE_LIMIT` (por defecto 1000 por día) es un tope sumando todas
  las IPs, para quien use muchas IPs distintas. Al llegar, el bot deriva al
  WhatsApp hasta el día siguiente.
- El historial que manda el widget se recorta a 12.000 caracteres, para
  que no se puedan inflar las consultas con conversaciones falsas.
- La última barrera es el **tope de gasto mensual** en console.anthropic.com.
- El contador del límite vive en la memoria de cada proceso. Con varios
  workers el límite real puede ser algo mayor; para algo estricto, usá
  Redis (`RATELIMIT_STORAGE_URI=redis://...`).
- El widget guarda el historial de la conversación en `sessionStorage`
  del navegador del cliente (se borra al cerrar la pestaña).

## Registro de consultas

Cada consulta queda registrada como una línea JSON con fecha, IP,
pregunta, respuesta, búsquedas que hizo el bot en el catálogo, tiempo de
respuesta y tokens usados. Se ve de dos formas:

```bash
docker compose logs -f | grep "chat:"      # en vivo
tail -f logs/chats.jsonl                   # archivo en el servidor
```

El archivo `logs/chats.jsonl` queda en el servidor aunque recrees el
contenedor. Crece ~1-2 KB por consulta y no se rota solo: borralo o
archivalo cada tanto. Tené en cuenta que guarda lo que escriben los
clientes; para no guardarlo en archivo, poné `CHAT_LOG_FILE=` vacío en
`.env`.

Para ver las búsquedas que no encontraron nada (productos que la gente
pide y el bot no encuentra):

```bash
grep '"results": 0' logs/chats.jsonl
```

## Panel de revisión (`/admin`)

En `https://chat.lsydstore.com.ar/admin` una persona puede revisar las
respuestas del bot y corregirlas. Se activa poniendo en `.env` una
contraseña larga:

```
ADMIN_USER=admin
ADMIN_PASSWORD=una-contraseña-larga-y-única
```

Sin `ADMIN_PASSWORD`, `/admin` no existe. El navegador pide usuario y
contraseña (autenticación básica, así que usalo solo por HTTPS).

Qué muestra:

- Métricas del período: consultas, conversaciones, errores, tiempo de
  respuesta, tokens y cuántas respuestas se revisaron.
- Consultas por día, búsquedas que no encontraron nada y búsquedas más hechas.
- Cada pregunta con la respuesta del bot, filtrable (sin revisar, mal,
  con error, sin resultados...) y con la conversación completa del cliente.

En cada respuesta se puede marcar **✓ Bien** o **✗ Mal** y escribir la
**respuesta correcta**. Si se deja tildado "Que el bot use esta
corrección", el bot la recibe en sus instrucciones desde la siguiente
consulta y responde parecido cuando le preguntan algo similar (no hace
falta reiniciar). En la pestaña "Correcciones que usa el bot" se pueden
desactivar o borrar.

Las revisiones se guardan en `logs/reviews.json` (cambiá la ruta con
`REVIEWS_FILE`). Para respuestas generales de la tienda (pagos, envíos...)
es mejor cargarlas en `faq/faq.md`; las correcciones sirven para casos
puntuales que salen de revisar charlas reales.
