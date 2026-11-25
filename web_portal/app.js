"use strict";

(() => {
  const DEFAULT_OAUTH_PORT = 8888;
  const MIN_LOOPBACK_PORT = 1024;
  const MAX_LOOPBACK_PORT = 65535;
  const ALLOWED_CALLBACK_PATHS = new Set(["/callback", "/setup-sync", "/health"]);
  const STATE_TOKEN_RE = /^[A-Za-z0-9._~-]{16,256}$/;
  const SYNC_TOKEN_RE = /^[A-Za-z0-9._~-]{16,256}$/;
  const PROVIDER_RE = /^[a-z0-9_-]{2,32}$/;

  /**
   * Valida y normaliza un puerto loopback seguro dentro de [1024, 65535].
   * @param {string|number|null|undefined} rawPort
   * @returns {number}
   */
  function parseSafePort(rawPort) {
    if (rawPort === null || rawPort === undefined || rawPort === "") {
      return DEFAULT_OAUTH_PORT;
    }
    const text = String(rawPort).trim();
    if (!/^\d{4,5}$/.test(text)) {
      throw new Error("Puerto local inválido. Debe ser un número entero entre 1024 y 65535.");
    }
    const port = Number.parseInt(text, 10);
    if (!Number.isInteger(port) || port < MIN_LOOPBACK_PORT || port > MAX_LOOPBACK_PORT) {
      throw new Error("Puerto fuera del rango permitido (1024–65535).");
    }
    return port;
  }

  /**
   * Construye una URL estricta hacia http://127.0.0.1:<puerto><ruta>.
   * Bloquea Open-Redirect, esquemas arbitrarios, credenciales en URL y dominios externos.
   * @param {string|number|null|undefined} rawPort
   * @param {string} path
   * @returns {URL}
   */
  function buildLoopbackUrl(rawPort, path) {
    const port = parseSafePort(rawPort);
    if (typeof path !== "string" || !ALLOWED_CALLBACK_PATHS.has(path)) {
      throw new Error("Ruta loopback no autorizada.");
    }
    if (path.includes("//") || path.includes("\\") || path.includes("@")) {
      throw new Error("Ruta loopback malformada.");
    }
    const built = new URL(`http://127.0.0.1:${port}${path}`);
    if (
      built.protocol !== "http:" ||
      built.hostname !== "127.0.0.1" ||
      built.port !== String(port) ||
      built.username !== "" ||
      built.password !== "" ||
      built.pathname !== path
    ) {
      throw new Error("Validación anti Open-Redirect rechazada.");
    }
    return built;
  }

  /**
   * Valida formato seguro del parámetro anti-CSRF `state`.
   * @param {string|null|undefined} state
   * @returns {string}
   */
  function validateOAuthState(state) {
    const clean = typeof state === "string" ? state.trim() : "";
    if (!STATE_TOKEN_RE.test(clean)) {
      throw new Error("El parámetro anti-CSRF 'state' falta o tiene un formato inválido.");
    }
    return clean;
  }

  /**
   * Valida token efímero de sesión para Setup Sync.
   * @param {string|null|undefined} token
   * @returns {string}
   */
  function validateSyncToken(token) {
    const clean = typeof token === "string" ? token.trim() : "";
    if (!SYNC_TOKEN_RE.test(clean)) {
      throw new Error("Token efímero de Setup Sync inválido o faltante.");
    }
    return clean;
  }

  /**
   * Extrae parámetros desde el fragmento (#...) sin enviarlos al servidor remoto.
   * @returns {URLSearchParams}
   */
  function parseHashParams() {
    const rawHash = window.location.hash.startsWith("#")
      ? window.location.hash.slice(1)
      : window.location.hash;
    return new URLSearchParams(rawHash);
  }

  /**
   * Verifica disponibilidad del servidor local DeusEx en http://127.0.0.1:<port>/health.
   * @param {number|string} rawPort
   * @param {number} timeoutMs
   * @returns {Promise<{online: boolean, port: number, payload: object|null, error: string|null}>}
   */
  async function checkLocalHealth(rawPort = DEFAULT_OAUTH_PORT, timeoutMs = 2500) {
    let url;
    try {
      url = buildLoopbackUrl(rawPort, "/health");
    } catch (err) {
      return {
        online: false,
        port: DEFAULT_OAUTH_PORT,
        payload: null,
        error: err instanceof Error ? err.message : "Puerto inválido.",
      };
    }

    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url.toString(), {
        method: "GET",
        mode: "cors",
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) {
        return {
          online: false,
          port: Number.parseInt(url.port, 10),
          payload: null,
          error: `DeusEx respondió con HTTP ${response.status}.`,
        };
      }
      const data = await response.json();
      return {
        online: Boolean(data && data.ok),
        port: Number.parseInt(url.port, 10),
        payload: data,
        error: null,
      };
    } catch (_err) {
      return {
        online: false,
        port: Number.parseInt(url.port, 10),
        payload: null,
        error: "No se detectó DeusEx local escuchando en 127.0.0.1:" + url.port + ".",
      };
    } finally {
      window.clearTimeout(timer);
    }
  }

  /**
   * Envía claves o ajustes de configuración directo a DeusEx local vía Setup Sync.
   * Jamás envía credenciales fuera de 127.0.0.1.
   * @param {{port?: number|string, syncToken: string, secrets?: Record<string, string>, settings?: Record<string, unknown>}} options
   * @returns {Promise<{ok: boolean, status: number, body: object|null, message: string}>}
   */
  async function sendSetupSync(options) {
    const url = buildLoopbackUrl(options.port, "/setup-sync");
    const syncToken = validateSyncToken(options.syncToken);

    const payload = {
      secrets: options.secrets || {},
      settings: options.settings || {},
    };

    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 4500);
    try {
      const response = await fetch(url.toString(), {
        method: "POST",
        mode: "cors",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "X-DeusEx-Sync-Token": syncToken,
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      let body = null;
      try {
        body = await response.json();
      } catch (_jsonErr) {
        body = null;
      }
      if (!response.ok) {
        const msg =
          (body && typeof body.error === "string" && body.error) ||
          `Error HTTP ${response.status} al sincronizar con DeusEx local.`;
        return { ok: false, status: response.status, body, message: msg };
      }
      return {
        ok: true,
        status: response.status,
        body,
        message:
          (body && typeof body.message === "string" && body.message) ||
          "Configuración sincronizada con éxito en DeusEx local.",
      };
    } catch (_err) {
      return {
        ok: false,
        status: 0,
        body: null,
        message:
          "No se pudo conectar con DeusEx local en 127.0.0.1:" +
          url.port +
          ". Abre Integraciones -> Setup Sync en tu PC e inténtalo nuevamente.",
      };
    } finally {
      window.clearTimeout(timer);
    }
  }

  /**
   * Construye la URL de reenvío OAuth 2.0 PKCE únicamente hacia http://127.0.0.1:<port>/callback.
   * Rechaza cualquier intento de redirección a host externo o sin `state` válido.
   * @param {{port?: string|number|null, code?: string|null, state?: string|null, error?: string|null, errorDescription?: string|null, provider?: string|null}} params
   * @returns {string}
   */
  function buildOAuthLoopbackForwardUrl(params) {
    const target = buildLoopbackUrl(params.port, "/callback");
    const validState = validateOAuthState(params.state);
    target.searchParams.set("state", validState);

    if (params.provider && PROVIDER_RE.test(params.provider.trim().toLowerCase())) {
      target.searchParams.set("provider", params.provider.trim().toLowerCase());
    }
    if (params.error) {
      target.searchParams.set("error", String(params.error).slice(0, 128));
      if (params.errorDescription) {
        target.searchParams.set(
          "error_description",
          String(params.errorDescription).slice(0, 256)
        );
      }
    } else if (params.code && String(params.code).trim().length > 0) {
      target.searchParams.set("code", String(params.code).trim());
    } else {
      throw new Error("Falta el código de autorización OAuth ('code') o error del proveedor.");
    }
    return target.toString();
  }

  /**
   * Resuelve el enlace de GitHub Releases automáticamente cuando el sitio vive en *.github.io.
   */
  function hydrateGithubLinks() {
    const host = window.location.hostname.toLowerCase();
    if (!host.endsWith(".github.io")) {
      return;
    }
    const owner = host.replace(/\.github\.io$/, "");
    const segments = window.location.pathname.split("/").filter(Boolean);
    const repo = segments.length > 0 ? segments[0] : "deusex-v3";
    if (!owner || !repo) {
      return;
    }
    const repoUrl = `https://github.com/${owner}/${repo}`;
    const releasesUrl = `${repoUrl}/releases/latest`;

    document.querySelectorAll("[data-github-releases]").forEach((el) => {
      if (el instanceof HTMLAnchorElement) {
        el.href = releasesUrl;
      }
    });
    document.querySelectorAll("[data-github-repo]").forEach((el) => {
      if (el instanceof HTMLAnchorElement) {
        el.href = repoUrl;
      }
    });
  }

  function wireNavigation() {
    const toggle = document.querySelector("[data-nav-toggle]");
    const menu = document.querySelector("[data-nav-links]");
    if (toggle && menu) {
      toggle.addEventListener("click", () => {
        const isOpen = menu.classList.toggle("open");
        toggle.setAttribute("aria-expanded", String(isOpen));
      });
    }
  }

  function wireCopyButtons() {
    document.querySelectorAll("[data-copy]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const text = btn.getAttribute("data-copy") || "";
        if (!text || !navigator.clipboard) {
          return;
        }
        const original = btn.textContent;
        try {
          await navigator.clipboard.writeText(text);
          btn.textContent = "Copiado";
          window.setTimeout(() => {
            btn.textContent = original;
          }, 1600);
        } catch (_err) {
          btn.textContent = "Error al copiar";
        }
      });
    });
  }

  function wireHealthProbeWidgets() {
    document.querySelectorAll("[data-check-local-btn]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const pill = document.querySelector("[data-local-status-pill]");
        const portInput = document.querySelector("[data-local-port-input]");
        const port =
          portInput instanceof HTMLInputElement && portInput.value.trim()
            ? portInput.value.trim()
            : DEFAULT_OAUTH_PORT;

        if (pill) {
          pill.setAttribute("data-state", "info");
          pill.textContent = `Verificando 127.0.0.1:${port}...`;
        }
        const res = await checkLocalHealth(port);
        if (pill) {
          if (res.online) {
            pill.setAttribute("data-state", "ok");
            pill.textContent = `DeusEx local conectado (127.0.0.1:${res.port})`;
          } else {
            pill.setAttribute("data-state", "warn");
            pill.textContent = res.error || "DeusEx local no detectado";
          }
        }
      });
    });
  }

  function wireGuidesSetupSyncForm() {
    const form = document.querySelector("[data-setup-sync-form]");
    if (!(form instanceof HTMLFormElement)) {
      return;
    }

    const portInput = form.querySelector("[name='sync_port']");
    const tokenInput = form.querySelector("[name='sync_token']");
    const geminiInput = form.querySelector("[name='gemini_api_key']");
    const discordInput = form.querySelector("[name='discord_bot_token']");
    const statusBox = form.querySelector("[data-setup-sync-status]");
    const pill = document.querySelector("[data-local-status-pill]");

    // Pre-rellena puerto y token efímero si DeusEx abrió la guía usando fragmento #sync_token=...
    const hashParams = parseHashParams();
    const hashToken = hashParams.get("sync_token");
    const hashPort = hashParams.get("port");
    if (portInput instanceof HTMLInputElement && hashPort) {
      try {
        portInput.value = String(parseSafePort(hashPort));
      } catch (_err) {
        portInput.value = String(DEFAULT_OAUTH_PORT);
      }
    }
    if (tokenInput instanceof HTMLInputElement && hashToken) {
      tokenInput.value = hashToken.trim();
      // Limpia el fragmento del historial para no dejar el token efímero visible en la barra URL.
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
      checkLocalHealth(portInput instanceof HTMLInputElement ? portInput.value : DEFAULT_OAUTH_PORT)
        .then((res) => {
          if (!pill) return;
          pill.setAttribute("data-state", res.online ? "ok" : "warn");
          pill.textContent = res.online
            ? `Sesión Setup Sync lista (127.0.0.1:${res.port})`
            : res.error || "DeusEx local no detectado";
        });
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!(statusBox instanceof HTMLElement)) {
        return;
      }

      const rawPort = portInput instanceof HTMLInputElement ? portInput.value : DEFAULT_OAUTH_PORT;
      const syncToken = tokenInput instanceof HTMLInputElement ? tokenInput.value.trim() : "";
      const secrets = {};

      if (geminiInput instanceof HTMLInputElement && geminiInput.value.trim()) {
        secrets.GEMINI_API_KEY = geminiInput.value.trim();
      }
      if (discordInput instanceof HTMLInputElement && discordInput.value.trim()) {
        secrets.DISCORD_BOT_TOKEN = discordInput.value.trim();
      }

      if (Object.keys(secrets).length === 0) {
        statusBox.hidden = false;
        statusBox.setAttribute("data-type", "warn");
        statusBox.textContent = "Ingresa al menos una clave (Gemini o Discord) antes de sincronizar.";
        return;
      }

      statusBox.hidden = false;
      statusBox.setAttribute("data-type", "info");
      statusBox.textContent = "Enviando credenciales de forma local hacia 127.0.0.1...";

      try {
        const result = await sendSetupSync({
          port: rawPort,
          syncToken,
          secrets,
        });
        statusBox.setAttribute("data-type", result.ok ? "ok" : "error");
        statusBox.textContent = result.message;
        if (result.ok) {
          if (geminiInput instanceof HTMLInputElement) geminiInput.value = "";
          if (discordInput instanceof HTMLInputElement) discordInput.value = "";
        }
      } catch (err) {
        statusBox.setAttribute("data-type", "error");
        statusBox.textContent =
          err instanceof Error ? err.message : "Error de validación al enviar Setup Sync.";
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    hydrateGithubLinks();
    wireNavigation();
    wireCopyButtons();
    wireHealthProbeWidgets();
    wireGuidesSetupSyncForm();
  });

  window.DeusExPortal = Object.freeze({
    DEFAULT_OAUTH_PORT,
    parseSafePort,
    buildLoopbackUrl,
    validateOAuthState,
    validateSyncToken,
    parseHashParams,
    checkLocalHealth,
    sendSetupSync,
    buildOAuthLoopbackForwardUrl,
  });
})();
