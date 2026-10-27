/**
 * Widget de chat de LSyD Store.
 *
 * Uso: pegar antes de </body> en el sitio.
 *   <script src="https://TU-BACKEND/widget/lsydstore-chatbot.js"
 *           data-api-url="https://TU-BACKEND"
 *           data-turnstile-sitekey="SITE-KEY-DE-TURNSTILE"></script>
 *
 * data-api-url: URL base del backend (donde corre app.py). Requerido.
 * data-turnstile-sitekey: site key de Cloudflare Turnstile. Requerido si el
 *   backend tiene TURNSTILE_SECRET_KEY (si no, el chat responde 401).
 */
(function () {
  "use strict";

  var scriptTag = document.currentScript;
  var API_URL = (scriptTag && scriptTag.getAttribute("data-api-url")) || "";
  if (!API_URL) {
    console.error("[lsydstore-chatbot] Falta data-api-url en el <script> del widget.");
    return;
  }
  API_URL = API_URL.replace(/\/$/, "");
  var TURNSTILE_SITEKEY = (scriptTag && scriptTag.getAttribute("data-turnstile-sitekey")) || "";

  var STORAGE_KEY = "lsyd_chat";
  // La conversación se guarda en localStorage para que siga al pasar de
  // página o abrir un producto en otra pestaña; se descarta tras 24 h sin uso.
  var STORAGE_TTL = 24 * 60 * 60 * 1000;
  var SESSION_STORAGE_KEY = "lsyd_chat_session";

  var css =
    "#lsyd-chat-launcher{position:fixed;bottom:20px;right:20px;width:60px;height:60px;" +
    "border-radius:50%;background:#1a1a1a;color:#fff;border:none;cursor:pointer;" +
    "box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:999999;display:flex;align-items:center;" +
    "justify-content:center;transition:transform .15s ease;}" +
    "#lsyd-chat-launcher:hover{transform:scale(1.06);}" +
    "#lsyd-chat-launcher svg{width:28px;height:28px;fill:#fff;}" +
    "#lsyd-chat-panel{position:fixed;bottom:90px;right:20px;width:360px;max-width:92vw;" +
    "height:520px;max-height:75vh;background:#fff;border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.25);" +
    "display:none;flex-direction:column;overflow:hidden;z-index:999999;" +
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;}" +
    "#lsyd-chat-panel.open{display:flex;}" +
    "#lsyd-chat-header{background:#1a1a1a;color:#fff;padding:14px 16px;display:flex;" +
    "align-items:center;justify-content:space-between;flex-shrink:0;}" +
    "#lsyd-chat-header .title{font-weight:600;font-size:15px;}" +
    "#lsyd-chat-header .subtitle{font-size:11.5px;opacity:.85;margin-top:2px;}" +
    "#lsyd-chat-close{background:none;border:none;color:#fff;font-size:20px;cursor:pointer;" +
    "line-height:1;padding:4px;}" +
    "#lsyd-chat-messages{flex:1;overflow-y:auto;padding:14px;background:#fafafa;}" +
    ".lsyd-msg{max-width:85%;margin-bottom:10px;padding:9px 12px;border-radius:12px;" +
    "font-size:13.5px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word;}" +
    ".lsyd-msg.user{background:#1a1a1a;color:#fff;margin-left:auto;border-bottom-right-radius:3px;}" +
    ".lsyd-msg.bot{background:#f5ede6;color:#333;margin-right:auto;border-bottom-left-radius:3px;}" +
    ".lsyd-msg.bot a{color:#b87e4f;font-weight:600;text-decoration:underline;}" +
    ".lsyd-product{display:flex;align-items:center;gap:10px;margin:4px 0;padding:6px;" +
    "background:#fff;border-radius:10px;text-decoration:none!important;}" +
    ".lsyd-product img{width:64px;height:64px;object-fit:cover;border-radius:8px;flex-shrink:0;background:#f3f3f3;}" +
    ".lsyd-product span{font-size:12.5px;}" +
    ".lsyd-msg.error{background:#fbe4e4;color:#8a2020;margin-right:auto;}" +
    ".lsyd-typing{display:flex;gap:4px;padding:9px 12px;}" +
    ".lsyd-typing span{width:6px;height:6px;border-radius:50%;background:#1a1a1a;opacity:.5;" +
    "animation:lsyd-bounce 1s infinite;}" +
    ".lsyd-typing span:nth-child(2){animation-delay:.15s;}" +
    ".lsyd-typing span:nth-child(3){animation-delay:.3s;}" +
    "@keyframes lsyd-bounce{0%,60%,100%{transform:translateY(0);opacity:.5;}" +
    "30%{transform:translateY(-4px);opacity:1;}}" +
    "#lsyd-chat-turnstile{display:flex;justify-content:center;flex-shrink:0;}" +
    "#lsyd-chat-form{display:flex;border-top:1px solid #eee;padding:8px;flex-shrink:0;background:#fff;}" +
    "#lsyd-chat-input{flex:1;border:1px solid #ddd;border-radius:20px;padding:9px 14px;" +
    "font-size:13.5px;outline:none;resize:none;font-family:inherit;}" +
    "#lsyd-chat-input:focus{border-color:#1a1a1a;}" +
    "#lsyd-chat-send{background:#1a1a1a;border:none;color:#fff;width:38px;height:38px;" +
    "border-radius:50%;margin-left:8px;cursor:pointer;flex-shrink:0;display:flex;" +
    "align-items:center;justify-content:center;}" +
    "#lsyd-chat-send:disabled{opacity:.5;cursor:default;}" +
    "#lsyd-chat-send svg{width:16px;height:16px;fill:#fff;}";

  var styleEl = document.createElement("style");
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  var launcher = document.createElement("button");
  launcher.id = "lsyd-chat-launcher";
  launcher.setAttribute("aria-label", "Abrir chat de ayuda");
  launcher.innerHTML =
    '<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.03 2 11c0 2.4 1.05 4.58 2.78 6.19' +
    "L4 22l5.1-1.35c.9.23 1.87.35 2.9.35 5.52 0 10-4.03 10-9S17.52 2 12 2z\"/></svg>";

  var panel = document.createElement("div");
  panel.id = "lsyd-chat-panel";
  panel.innerHTML =
    '<div id="lsyd-chat-header">' +
    '<div><div class="title">Asistente LSyD</div>' +
    '<div class="subtitle">Te ayudo a encontrar el producto ideal</div></div>' +
    '<button id="lsyd-chat-close" aria-label="Cerrar chat">&times;</button>' +
    "</div>" +
    '<div id="lsyd-chat-messages"></div>' +
    // Turnstile solo se ve acá si Cloudflare le pide al cliente que haga clic
    '<div id="lsyd-chat-turnstile"></div>' +
    '<form id="lsyd-chat-form">' +
    '<textarea id="lsyd-chat-input" rows="1" placeholder="Escribí tu consulta..." maxlength="2000"></textarea>' +
    '<button id="lsyd-chat-send" type="submit" aria-label="Enviar">' +
    '<svg viewBox="0 0 24 24"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg>' +
    "</button>" +
    "</form>";

  document.body.appendChild(launcher);
  document.body.appendChild(panel);

  var messagesEl = panel.querySelector("#lsyd-chat-messages");
  var formEl = panel.querySelector("#lsyd-chat-form");
  var inputEl = panel.querySelector("#lsyd-chat-input");
  var sendBtn = panel.querySelector("#lsyd-chat-send");
  var closeBtn = panel.querySelector("#lsyd-chat-close");
  var turnstileEl = panel.querySelector("#lsyd-chat-turnstile");

  function loadState() {
    try {
      var state = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (state && Array.isArray(state.history) && state.saved > Date.now() - STORAGE_TTL) return state;
    } catch (e) {
      /* almacenamiento no disponible o dañado: se empieza de cero */
    }
    return {};
  }

  var state = loadState();
  var history = state.history || [];
  // Id al azar de esta conversación: el panel /admin lo usa para mostrar
  // juntas las consultas de un mismo cliente.
  var conversationId =
    state.conversation || Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
  var isOpen = !!state.open;

  function saveState() {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ history: history, conversation: conversationId, open: isOpen, saved: Date.now() })
      );
    } catch (e) {
      /* almacenamiento no disponible, seguimos sin persistir */
    }
  }

  // --- Verificación con Cloudflare Turnstile ---
  // El widget demuestra que es un navegador real y el backend le devuelve una
  // sesión de 30 minutos que se manda con cada consulta.

  var GENERIC_ERROR = "No pudimos conectar con el asistente. Probá de nuevo.";
  var turnstileLoading = null;
  var turnstileWidgetId = null;
  var pendingTurnstile = null;
  var sessionInFlight = null;

  function loadTurnstile() {
    if (!turnstileLoading) {
      turnstileLoading = new Promise(function (resolve, reject) {
        if (window.turnstile) return resolve(window.turnstile);
        var s = document.createElement("script");
        s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
        s.async = true;
        s.onload = function () {
          resolve(window.turnstile);
        };
        s.onerror = function () {
          turnstileLoading = null;
          reject(new Error(GENERIC_ERROR));
        };
        document.head.appendChild(s);
      });
    }
    return turnstileLoading;
  }

  function settleTurnstile(err, token) {
    var pending = pendingTurnstile;
    pendingTurnstile = null;
    if (!pending) return;
    if (err) pending.reject(err);
    else pending.resolve(token);
  }

  function getTurnstileToken() {
    return loadTurnstile().then(function (turnstile) {
      return new Promise(function (resolve, reject) {
        pendingTurnstile = { resolve: resolve, reject: reject };
        if (turnstileWidgetId === null) {
          turnstileWidgetId = turnstile.render(turnstileEl, {
            sitekey: TURNSTILE_SITEKEY,
            appearance: "interaction-only",
            callback: function (token) {
              settleTurnstile(null, token);
            },
            "error-callback": function () {
              settleTurnstile(new Error("No pudimos verificar tu navegador. Recargá la página y probá de nuevo."));
            },
          });
        } else {
          turnstile.reset(turnstileWidgetId);
        }
      });
    });
  }

  function loadSession() {
    try {
      var s = JSON.parse(sessionStorage.getItem(SESSION_STORAGE_KEY) || "null");
      // Margen de 1 minuto para no mandar una sesión a punto de vencer
      return s && s.token && s.expires > Date.now() + 60000 ? s.token : null;
    } catch (e) {
      return null;
    }
  }

  function saveSession(token, expiresIn) {
    try {
      sessionStorage.setItem(
        SESSION_STORAGE_KEY,
        JSON.stringify({ token: token, expires: Date.now() + expiresIn * 1000 })
      );
    } catch (e) {
      /* almacenamiento no disponible: se pide una sesión nueva cuando haga falta */
    }
  }

  function clearSession() {
    try {
      sessionStorage.removeItem(SESSION_STORAGE_KEY);
    } catch (e) {
      /* nada que borrar */
    }
  }

  // Devuelve una sesión válida, pidiendo una nueva si no hay o si forceNew
  function getSession(forceNew) {
    if (!TURNSTILE_SITEKEY) return Promise.resolve("");
    if (!forceNew) {
      var cached = loadSession();
      if (cached) return Promise.resolve(cached);
    }
    if (!sessionInFlight) {
      sessionInFlight = getTurnstileToken()
        .then(function (token) {
          return postJson("/api/session", { turnstile_token: token });
        })
        .then(function (data) {
          saveSession(data.session, data.expires_in);
          return data.session;
        })
        .finally(function () {
          sessionInFlight = null;
        });
    }
    return sessionInFlight;
  }

  function postJson(path, body) {
    return fetch(API_URL + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (res) {
      return res
        .json()
        .catch(function () {
          // Respuesta que no es JSON (ej. un 502 del hosting): no mostramos el error técnico
          throw new Error(GENERIC_ERROR);
        })
        .then(function (data) {
          if (!res.ok) {
            var err = new Error(data.error || "Error desconocido");
            err.code = data.code;
            throw err;
          }
          return data;
        });
    });
  }

  function sendChat(text) {
    function send(session) {
      // Al backend le alcanza con el texto: las fotos quedan solo en el navegador
      var turns = history.map(function (turn) {
        return { role: turn.role, content: turn.content };
      });
      return postJson("/api/chat", {
        message: text,
        history: turns,
        session: session,
        conversation: conversationId,
      });
    }
    return getSession(false)
      .then(send)
      .catch(function (err) {
        // Sesión vencida o de otra IP (ej. el celular cambió de red): se
        // pide una nueva y se reintenta una sola vez
        if (err.code !== "session_required") throw err;
        clearSession();
        return getSession(true).then(send);
      });
  }

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function bareHost(host) {
    return host.replace(/^www\./, "");
  }

  // Los links del propio sitio abren en la misma pestaña (el chat sigue
  // abierto en la página nueva); los de otros sitios, en una pestaña aparte.
  function isSameSite(url) {
    try {
      return bareHost(new URL(url).hostname) === bareHost(location.hostname);
    } catch (e) {
      return false;
    }
  }

  function linkify(escapedText) {
    // Sin comillas en la URL: si no, un texto como https://x"onmouseover=...
    // se saldría del atributo href.
    var urlPattern = /(https?:\/\/[^\s<"']+)/g;
    return escapedText.replace(urlPattern, function (url) {
      var clean = url.replace(/[.,;:!?)]+$/, "");
      var target = isSameSite(clean) ? "" : ' target="_blank" rel="noopener noreferrer"';
      return '<a href="' + clean + '"' + target + ">" + clean + "</a>";
    });
  }

  // Cambia el link de cada producto recomendado por una tarjeta con su foto
  function addProductImages(el, products) {
    var byUrl = {};
    (products || []).forEach(function (p) {
      if (p && p.url && typeof p.image === "string" && /^https:\/\//.test(p.image)) byUrl[p.url] = p;
    });
    Array.prototype.forEach.call(el.querySelectorAll("a"), function (a) {
      var product = byUrl[a.getAttribute("href")];
      if (!product) return;
      a.className = "lsyd-product";
      var img = document.createElement("img");
      img.src = product.image;
      img.alt = product.name || "";
      img.loading = "lazy";
      // Si la foto no carga, queda el link común
      img.onerror = function () {
        img.remove();
      };
      var label = document.createElement("span");
      label.textContent = "Ver producto";
      a.textContent = "";
      a.appendChild(img);
      a.appendChild(label);
      // La tarjeta ya ocupa su propia línea: el salto que venía después del link sobra
      var next = a.nextSibling;
      if (next && next.nodeType === 3) next.nodeValue = next.nodeValue.replace(/^\n/, "");
    });
  }

  function renderMessage(role, text, products) {
    var el = document.createElement("div");
    el.className = "lsyd-msg " + role;
    el.innerHTML = role === "bot" ? linkify(escapeHtml(text)) : escapeHtml(text);
    if (role === "bot") addProductImages(el, products);
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }

  function renderTyping() {
    var el = document.createElement("div");
    el.className = "lsyd-msg bot lsyd-typing-wrap";
    el.innerHTML = '<div class="lsyd-typing"><span></span><span></span><span></span></div>';
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }

  function renderExisting() {
    messagesEl.innerHTML = "";
    if (history.length === 0) {
      renderMessage(
        "bot",
        "¡Hola! Soy el asistente de LSyD Store. Contame qué estás buscando (mochila, cartera, valija, billetera...) y te ayudo a encontrarlo."
      );
      return;
    }
    history.forEach(function (turn) {
      renderMessage(turn.role === "user" ? "user" : "bot", turn.content, turn.products);
    });
  }

  renderExisting();

  function autoResize() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 100) + "px";
  }
  inputEl.addEventListener("input", autoResize);
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      formEl.requestSubmit();
    }
  });

  function toggleOpen(open, restoring) {
    panel.classList.toggle("open", open);
    isOpen = open;
    saveState();
    if (open) {
      // Al reabrirse solo después de cambiar de página no se enfoca, para
      // que en el celular no aparezca el teclado sin que el cliente lo pida
      if (!restoring) inputEl.focus();
      // Se verifica mientras el cliente escribe, así la primera respuesta no espera
      getSession(false).catch(function () {
        /* se reintenta al enviar el mensaje */
      });
    }
  }

  launcher.addEventListener("click", function () {
    toggleOpen(!panel.classList.contains("open"));
  });
  closeBtn.addEventListener("click", function () {
    toggleOpen(false);
  });

  // Si el chat estaba abierto en la página anterior, sigue abierto en esta
  if (isOpen) toggleOpen(true, true);

  formEl.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = inputEl.value.trim();
    if (!text) return;

    renderMessage("user", text);

    inputEl.value = "";
    autoResize();
    inputEl.disabled = true;
    sendBtn.disabled = true;
    var typingEl = renderTyping();

    sendChat(text)
      .then(function (data) {
        typingEl.remove();
        renderMessage("bot", data.reply, data.products);
        // Pregunta y respuesta se guardan juntas, así un error no deja el
        // historial desparejo.
        history.push({ role: "user", content: text });
        history.push({ role: "assistant", content: data.reply, products: data.products || [] });
        saveState();
      })
      .catch(function (err) {
        typingEl.remove();
        renderMessage("error", err.message || GENERIC_ERROR);
      })
      .finally(function () {
        inputEl.disabled = false;
        sendBtn.disabled = false;
        inputEl.focus();
      });
  });
})();
