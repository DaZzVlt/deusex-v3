# DeusEx v3 — Documento de Diseño Maestro y Arquitectura Comercial

**Versión:** 3.0.0 (Edición Comercial Comercializable)  
**Estado:** Congelado / Aprobado  
**Plataforma Objetivo:** Windows 11 (64-bit) + Companion APK (Android 10+)  
**Filosofía:** 100% Local-First, Zero-Base Execution (Completo desde el Minuto 1, Cero Consola/Configuración Técnica para el Usuario Final), Alta Estabilidad y Modularidad Enchufable.

---

## 1. Visión del Producto y Principios Innegociables

DeusEx v3 es un asistente personal autónomo y modular diseñado para funcionar como un centro de control unificado del PC, servicios en la nube, red local, mensajería y dispositivos móviles. Se distribuye como una aplicación comercial empaquetada e instalable con 1 clic en Windows 11.

### Principios Fundamentales
1. **Zero-Base Execution (Cero Fricción para Usuario Final):** El usuario instala la aplicación `.exe` y todo funciona de inmediato. No requiere instalar Python, Git, Node.js, FFmpeg ni ejecutar líneas de comando. Todos los binarios y modelos ligeros offline vienen pre-empaquetados en la distribución.
2. **Local-First y Privacidad:** Las configuraciones, credenciales, memoria adaptativa, alias de red y base de datos SQLite residen localmente en el equipo del usuario (`%LOCALAPPDATA%\DeusEx\`). No existe un servidor central de DeusEx capturando o procesando datos del usuario.
3. **Respuesta Rápida (Fast-Path Universal):** Todas las órdenes locales cotidianas (volumen, temporizadores, apertura de aplicaciones, privacidad, silencio, hora, perfiles) se resuelven en local en `<50 ms` con 0 consumo de tokens y sin depender de Internet.
4. **Resiliencia Operativa:** 0 procesos colgados, 0 hilos bloqueados, cierre determinista en `<3 s` y aislamiento estricto de errores: la falla de una API o servicio externo (ej. Spotify, Discord) jamás debe detener la interfaz ni el motor central.

---

## 2. Antídotos Técnicos Obligatorios (Lecciones de v2)

DeusEx v3 corrige de raíz los 5 fallos identificados en la auditoría técnica postmortem de la versión anterior:

1. **`ModelCatalogService.refresh(provider_id: str)`:** Resuelve internamente la instancia concreta del proveedor de IA antes de invocar la consulta de modelos, eliminando definitivamente el error `'str' object has no attribute 'list_models'`.
2. **Reconciliación de Estado en Arranque (`DeusExApplication.start()`):** Reconcilia automáticamente el estado de `ModuleRegistry` y `FastPathEngine` contra la configuración persistida en `settings.json` al arrancar. Ningún módulo habilitado en configuración inicia desactivado en frío.
3. **Catálogo Único `Permission(StrEnum)`:** Un solo enum central tipado para permisos compartido por configuración, motor de herramientas (`ToolRegistry`) y la UI. Prohibidos los hacks de modificación directa de atributos `object.__setattr__` en pruebas o producción.
4. **`AppScanner` Nativo sobre `shell:AppsFolder`:** Escaneo directo usando el shell nativo de Windows, resolviendo accesos directos (`.lnk`), aplicaciones UWP/MSIX (como Spotify UWP), rutas directas de ejecutables (`App Paths`), la aplicación predeterminada registrada en Windows (`UserChoice`) y una tabla de alias fonéticos. Elimina la pérdida de accesos directos.
5. **Enrutamiento Estricto de `EventBus` a `AsyncCoreThread`:** Toda corrutina o callback disparado por eventos se enruta obligatoriamente al bucle de eventos del hilo asíncrono principal, eliminando el fallo `RuntimeError: no running event loop` al finalizar la síntesis de voz (TTS) u operaciones asíncronas.

---

## 3. Arquitectura del Sistema y Modelo de Concurrencia

### Stack Tecnológico Unificado
* **Lenguaje & Entorno:** Python 3.14.10 gestionado mediante `uv` con un único `pyproject.toml` para desarrollo y empaquetado.
* **Interfaz Gráfica (GUI):** PySide6 6.12.0 (Qt 6) con aceleración por hardware.
* **Base de Datos:** SQLite 3 en modo WAL (Write-Ahead Logging) con bloqueo `RLock` por conexión.
* **Runtimes Aislados Pre-Empaquetados:**
  * **Vosk ES:** Modelo de voz offline en español (~40 MB) en `USER_DATA_DIR/models/vosk-es`.
  * **FFmpeg Portable:** Binario estático para procesamiento de audio en `USER_DATA_DIR/runtimes/ffmpeg`.
  * **Node.js + Baileys:** Runtime Node.js pre-instalado con `@whiskeysockets/baileys` en `USER_DATA_DIR/runtimes/whatsapp` (solo se ejecuta si el usuario activa el módulo de WhatsApp).

### Modelo Multihilo de 4 Capas (Sin Interbloqueos)
DeusEx v3 divide su ejecución en 4 hilos aislados con comunicación por señales Qt (`Signal/Slot`) y colas thread-safe:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          1. QtMainThread                                │
│  GUI (PySide6), Renderizado, Eventos de Usuario, Orbe, Hotkeys Globales │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ Signals / Slots
┌────────────────────────────────────▼────────────────────────────────────┐
│                        2. AsyncCoreThread                               │
│  asyncio EventLoop, Providers IA, EventBus, Fast-Path Engine, Core      │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ Thread-safe Queues
         ┌───────────────────────────┴───────────────────────────┐
         │                                                       │
┌────────▼──────────────────────────┐   ┌────────────────────────▼────────┐
│        3. ComStaThread            │   │       4. AudioInputThread       │
│ Single-Threaded Apartment (STA)   │   │ Captura continua de micrófono,  │
│ CoInitializeEx / pycaw / SAPI /   │   │ VAD, FarFieldAudioConditioner   │
│ uiautomation / Win+K Miracast     │   │ y motor Vosk ES offline         │
└───────────────────────────────────┘   └─────────────────────────────────┘
```

---

## 4. Estructura de Módulos y Funcionalidades por Bloque

### Bloque 1: UI/UX, 3 Superficies y Perfiles de Personalidad
* **Diseño Visual:** Estética Fluent Moderno de Windows 11. Soporte para temas Automático (sigue a Windows), Oscuro, Claro y `Cyber-HUD` con efecto Mica opcional. Formularios basados en tarjetas planas con `ToggleSwitch` y buscador en tiempo real en la vista de Ajustes.
* **Tres Superficies de Interacción:**
  1. **Orbe Flotante (Click-Through):** Oculto por defecto en reposo; se activa al recibir órdenes por voz o comandos. 5 estilos visuales seleccionables con vista previa fija de 4 segundos. Incluye `SubtitleHolobox` con renderizado de subtítulos palabra por palabra (bloques de máximo 14 palabras u 85 caracteres).
  2. **Mini-Chat (`Alt + Espacio`):** Ventana ligera emergente para comandos rápidos por teclado, confirmaciones de `SafetyGate` y acceso directo a Visión/Guía Live.
  3. **Ventana Principal Completa:** Dashboard con Sidebar navegable (Chat con historial completo, Módulos con paneles deslizables de configuración, Rutinas/Agenda, Memoria Adaptativa, Integraciones, Ajustes, Doctor del Sistema y Visor de Logs con función *"Copiar reporte para soporte"* anonimizado).
* **Perfiles y Tonos:** 5 personalidades base (DeusEx, Asistente Eficiente, Mentor Técnico, Compañero Relajado, Modo Cyberpunk) totalmente personalizables en nombre, prompt y tono de voz.
* **Onboarding en 4 Pasos:** Asistente inicial que configura nombre del usuario, proveedor de IA (soporta *Smart Clipboard Catcher* para autocompletar API keys), micrófono y vinculación OAuth en 1 clic.
* **Gestión de Costos (`CostGuard`):** Indicador discreto en el pie del Sidebar que calcula la estimación de consumo de tokens y muestra el detalle de topes por proveedor en Ajustes.

### Bloque 2: Cerebro, Proveedores de IA, Memoria y Seguridad
* **Motor Multimodelo:** Gemini (`google-genai`) como proveedor principal predeterminado. Adaptadores nativos `httpx` para llaves propias de OpenAI (ChatGPT), xAI (Grok), Anthropic (Claude), Groq, DeepSeek y OpenRouter.
* **Gestor de Proveedores (`ProviderPool`):** Auto-detector de modelos en modo `"Automático (Recomendado)"` (`FAST` para respuestas cortas/comandos y `DEEP` para análisis complejos). Monitoreo de latencia EMA por proveedor, limitador de ráfagas de concurrencia y cooldown exponencial antianomalías HTTP `429/503`.
* **Memoria Adaptativa en 2 Capas:** SQLite WAL con capa episódica (conversaciones recientes) y capa declarativa (hechos y preferencias del usuario). Píldora discreta en la UI *"Guardado en memoria · Deshacer"*. Se congela automáticamente al activar el **Modo Privacidad (`Ctrl + Shift + P`)**.
* **Fast-Path Universal:** Motor heurístico offline siempre activo (`<50 ms`, 0 tokens, reproducción de `Earcons`): resuelve alarmas, temporizadores, ajustes de sistema, perfiles, tono, silencio y hora local.
* **Seguridad y Control (`SafetyGate`):** Control físico de confirmación obligatoria previa para acciones destructivas o críticas (borrar archivos, enviar correos/mensajes, reiniciar/apagar el PC, modificar configuraciones de red).

### Bloque 3: Voz, Captura de Audio y Escudo Contextual
* **Motor de Escucha Offline:** Vosk ES pre-empaquetado para detección de Wake-Word y Push-to-Talk (PTT) por atajo global configurado mediante `GetAsyncKeyState` (sin hooks invasivos de sistema). Selector visual de micrófono con medidor de nivel en vivo.
* **Escudo Contextual Multicapa:**
  * `FarFieldAudioConditioner`: Filtro pasa-altos `>85 Hz`, AGC basado en `np.tanh` y VAD ajustado a voz humana (`300–3400 Hz`).
  * Sensibilidad dinámica con umbral elevado automáticamente cuando `is_multimedia_active` es reportado por `pycaw`.
  * `ContextGate` / `is_noise_or_third_person`: Filtro inteligente contra conversaciones secundarias o audio proveniente de altavoces.
  * Biometría Opcional `OwnerVoiceprintGuard`: Huella de voz de 26 dimensiones procesada en NumPy puro con proceso de calibración guiada de 6 segundos.
  * Ventana de seguimiento continuo (6 s) con filtro `[SILENCIO_CONTEXTUAL]` desactivada automáticamente en **Modo "En Llamada"**.
* **Síntesis de Voz (TTS) y Ducking:**
  * Edge-TTS mediante `QtMultimedia` (siempre activo por defecto) con fallback transparente a SAPI en `ComStaThread`.
  * Control de respuesta por voz (botón de silencio 1-clic para modo solo texto, velocidad ajustable, voz asignada por perfil y cancelación instantánea de síntesis al interrumpir el usuario — *barge-in*).
  * `SelectiveAudioDucker`: Atenúa automáticamente el volumen multimedia del sistema durante la síntesis, excluyendo aplicaciones de llamada activas (Discord, Teams, Zoom).

### Bloque 4: PC Local, Inteligencia Web y Visión
* **Control del PC:** `AppScanner` para apertura de apps (rutas nativas, UWP/MSIX, app predeterminada y alias fonéticos). Cierre inteligente (directo en apps cotidianas o juegos; retenido en `SafetyGate` para editores con trabajo no guardado). Control de volumen maestro/por app (`pycaw`) y brillo de pantalla. Gestión de ventanas multi-monitor DPI-aware. Búsqueda y organización de archivos con opción de **Deshacer (Undo)** en 1 clic. Telemetría de sistema sin requerir UAC (`psutil` + `nvidia-ml-py`). Creación y ejecución de **Rutinas** con opción *"Capturar estado actual"*.
* **Inteligencia Web (0 Claves Adicionales):** Búsqueda web (Gemini Search o DuckDuckGo + extractor `trafilatura` rápido/profundo) con capacidad de **abrir links directamente en el navegador del usuario** y **crear documentos de Google Docs** encadenados. Clima mediante Open-Meteo (tier gratuito con caché de 15 min), divisas, hora mundial y noticias RSS hiper-contextuales (filtradas por ubicación y Memoria Adaptativa). Extractores de contenido para URLs, PDFs, videos de YouTube y portapapeles.
* **Visión y Análisis de Pantalla:** Captura puntual multi-monitor DPI-aware con `mss`. **Modo Guía Live por turnos** (0 consumo de recursos ni procesamiento en pausa). Contrato de envío de capturas hacia Discord, WhatsApp y la APK Móvil.

### Bloque 5: Red Local, Smart TV y Música Universal
* **Descubrimiento Híbrido de Red:** Descubrimiento pasivo (mDNS / SSDP) combinado con **sondeo activo TCP/HTTP concurrente sobre la subred y tabla ARP local**, resolviendo direcciones MAC para Wake-on-LAN (WoL) en dispositivos como Chromecast, Samsung TV, LG TV y Xbox.
* **Adaptadores de Dispositivos LAN:**
  * Samsung Tizen TV (WebSocket + WoL).
  * Chromecast / Google TV.
  * Miracast (`Win + K` guiado por UIA en `ComStaThread` + `DisplaySwitch.exe` + cadena WoL).
  * YouTube TV Lounge API (prioritario para video web activo detectado mediante `ActiveBrowserUrlReader`).
  * Xbox SmartGlass.
* **Música Universal:**
  * Nivel 0: Integración universal con Windows SMTC (`winrt`).
  * **Spotify:** OAuth 2.0 PKCE (cuentas Premium y fallback Free) con lectura de *"Canciones que te gustan"*.
  * **TIDAL:** OAuth 2.0 PKCE oficial (`developer.tidal.com`).
  * **YouTube Music:** OAuth de Google (scope de YouTube lectura) + control remoto SMTC.
  * **Apple Music:** Lanzamiento vía URI `music://` + control SMTC.
  * **Biblioteca Local:** Indexación de archivos locales offline (`mutagen`).
  * **Puente `PlaylistBridgeEvent`:** Enrutamiento de reproducción directo hacia la app local o bot de Discord.

### Bloque 6: Portal Web, Google Workspace, Discord y WhatsApp
* **Portal Web Oficial (`web_portal/` en GitHub Pages):** Landing page comercial estática (`index.html`), guías ilustradas (`guides.html`), `privacy.html` (cumpliendo *Google API Services User Data Policy — Limited Use*), `terms.html`, `callback.html` (OAuth PKCE anti Open-Redirect) y **sistema Setup Sync que envía credenciales y tokens directamente a `127.0.0.1`** en el DeusEx local sin edición manual de archivos. Incluye credenciales pre-registradas en `oauth_app_defaults.json` (puerto `8888` para Spotify).
* **Google Workspace (`modules/workspace/`):** Integración OAuth 1-clic para Gmail (lectura, resúmenes, borradores), Google Calendar (CRUD de eventos), Google Tasks (CRUD de pendientes), Google Drive (búsqueda y descarga) y creación directa de Google Docs. Operaciones destructivas o de envío directo protegidas por `SafetyGate`.
* **Discord (`modules/discord/`):** Modelo de bot privado por usuario (guía paso a paso en el portal + botón de invitación 1-clic con Client ID auto-generado). Un único `DiscordGatewayService` administrando 2 módulos independientes:
  * `discord_chat`: Avisos del sistema, avisos de `SafetyGate`, control remoto del PC exclusivo para el rango Dueño (`!aprobar`, `!rechazar`, `!pendientes`) y envío de archivos/capturas.
  * `discord_music`: Arquitectura DAVE E2EE (`discord.py >= 2.6`, `davey`, `PyNaCl`, `audioop-lts`), FFmpeg pre-empaquetado, pre-descarga `yt-dlp`, búfer rodante de 3 pistas, medidor de energía RMS en NumPy puro (`MeteredPCMVolumeTransformer`), conjunto completo de comandos (`!play`, `!pause`, `!resume`, `!skip`, `!stop`, `!queue`, `!shuffle`, `!ahora`, `!volumen`, `!salir`, `!help`), rangos de usuario (`Dueño`, `DJ`, `Invitado`) y **modo `shuffle` estrictamente bajo demanda manual**.
* **WhatsApp (`modules/whatsapp/`):** Runtime aislado Node.js + `@whiskeysockets/baileys` pre-instalado en `USER_DATA_DIR/runtimes/whatsapp`. Vinculación visual por código QR dentro de la UI. Procesa únicamente mensajes nuevos (ignora historial antiguo y descarta estados/canales con `shouldIgnoreJid`). Transcripción y resumen de **notas de voz (audio `.ogg`/Opus) enviadas directamente a Gemini multimodal**. Envíos salientes retenidos en `SafetyGate`.

### Bloque 7: App Móvil Autónoma, Smartwatch y Empaquetado
* **App Móvil (`DeusEx Companion.apk`):** Aplicación Android nativa desarrollada en **Kotlin + Jetpack Compose** (Android 10+). Cero ADB. Vinculación en 10 segundos escaneando el código QR generado por el PC (`bridge/ws_server.py` en Wi-Fi local). Sincroniza tema visual, perfil activo, estilo del Orbe y accesos a Rutinas.
  * **En Wi-Fi LAN:** Control remoto del PC, recepción de capturas de pantalla al instante y panel de aprobación/rechazo de `SafetyGate`.
  * **En Datos Móviles (Mini-DeusEx Local):** Control local del smartphone (volumen, brillo, linterna, abrir apps, música local, alarmas) y chat directo mediante clave de IA configurada en la app.
* **Smartwatch Universal:** Lectura de telemetría de salud (pasos, pulso, sueño) a través de **Health Connect** en Android. Notificaciones nativas interactivas en el reloj para confirmar o rechazar acciones de `SafetyGate` y responder temporizadores.
* **Pulido Comercial y Empaquetado:**
  * **Doctor en 1 Clic:** Diagnóstico integral de dependencias, permisos, micrófono, modelos, runtimes y red con soluciones claras en español.
  * **Respaldo `.zip`:** Exportación e importación completa de configuraciones, perfiles, memoria adaptativa, rutinas y alias de red; excluye automáticamente `.env` y llaves privadas.
  * **Actualizaciones:** Chequeo silencioso contra GitHub Releases al iniciar.
  * **Instalador de Windows:** Paquete ejecutable único compilado con `PyInstaller --onedir` + Inno Setup (~180–250 MB) con Python 3.14, Vosk ES, FFmpeg portable y Node/Baileys integrados. Opción de arranque minimizado con Windows.

---

## 5. Flujo de Trabajo Operativo de Desarrollo

Para garantizar el cumplimiento de los estándares de producción, el desarrollo se organiza en **4 roles estructurados** y **5 compuertas de calidad obligatorias**:

### Matriz de Roles
1. **Chat de Diseño (Producto):** Custodio de la visión comercial, UX y arquitectura (`DESIGN_V3.md`). Evaluador de cambios de alcance.
2. **Chat Orquestador Principal:** Diseñador técnico rebanada por rebanada. Emite especificaciones en micro-lotes (3–5 archivos por entrega), audita salidas técnicas y autoriza el avance.
3. **Brazo Ejecutor en Disco (Google Antigravity):** Escribe código, aplica parches quirúrgicos en el repositorio local (`deusex-v3`), ejecuta entornos de prueba y verifica linters/tests.
4. **Director y Validador Real (Diego):** Prueba la aplicación en su entorno real de Windows 11, valida la experiencia de usuario y congela los hitos con `git commit` y `git tag`.

### Las 5 Compuertas Obligatorias por Rebanada
```
 ┌─────────────────────────────────────────────────────────┐
 │ Compuerta 1: Contrato y Especificación (Orquestador)    │
 └────────────────────────────┬────────────────────────────┘
                              ▼
 ┌─────────────────────────────────────────────────────────┐
 │ Compuerta 2: Integración Limpia en Disco (Antigravity) │
 └────────────────────────────┬────────────────────────────┘
                              ▼
 ┌─────────────────────────────────────────────────────────┐
 │ Compuerta 3: Validaciones Offline (Antigravity)         │
 │ (pytest -m "not live", ruff check, mypy -> código 0)   │
 └────────────────────────────┬────────────────────────────┘
                              ▼
 ┌─────────────────────────────────────────────────────────┐
 │ Compuerta 4: Prueba en Vivo Real en Windows 11 (Diego)  │
 │ (Smoke test en vivo + verificación visual en la GUI)    │
 └────────────────────────────┬────────────────────────────┘
                              ▼
 ┌─────────────────────────────────────────────────────────┐
 │ Compuerta 5: Handoff y Checkpoint Git (Diego -> Tag)    │
 └─────────────────────────────────────────────────────────┘
```

---

## 6. Plan Maestro de Rebanadas Verticales Reales

El desarrollo se ejecutará secuencialmente en 11 rebanadas funcionales autosuficientes (Rebanadas 0 a 10):

| Rebanada | Nombre / Alcance | Entregables Principales | Criterio de Cierre (Prueba en Vivo) |
| :-: | :--- | :--- | :--- |
| **0** | **Cimientos, Stack y Permisos** | `pyproject.toml` (Py 3.14), `setup_runtimes.py`, `paths.py`, `permissions.py`, `config.py` + `.env`, `db.py` (WAL), `runtime.py` (4 hilos), `events.py`. | `uv sync` + `setup_runtimes.py` en código 0. Cierre determinista de 4 hilos en `<3 s`. |
| **1** | **Shell Fluent + Cerebro Gemini + Onboarding** | `MainWindow` Fluent, `MiniChat`, `Tray`, Privacidad, Temas/Mica, Perfiles, Onboarding + *Smart Clipboard Catcher*, `SafetyGate`, `CostGuard`, `MemoryManager`, `ProviderPool`. | API key en caliente lista ~19 modelos reales. Chat en vivo con memoria SQLite, temporizador Fast-Path y cierre `<3 s`. |
| **2** | **PC Local, `shell:AppsFolder` y Rutinas** | `PCModule`, `AppScanner` (`shell:AppsFolder` + `UserChoice` + alias), volumen `pycaw`, brillo, multi-monitor, archivos Undo, telemetría, Rutinas. | `"abre spotify"`, `"abre el navegador"`, `"pon volumen al 35"` responden en `<50 ms`. Reconciliación de estado en reinicio OK. |
| **3** | **Red Local Híbrida + Smart TV / Cast / Xbox** | `NetworkDiscoveryService` (mDNS/SSDP + sondeo ARP/subred + MAC WoL), adaptadores Tizen, Chromecast, Miracast, YouTube TV Lounge, Xbox. | Escaneo detecta dispositivos reales en LAN sin IPs fijas. Control multimedia y proyección funcionando desde la UI. |
| **4** | **Orbe Flotante, Voz, Escudo Contextual y Ducking** | `FloatingOrbWindow` (5 estilos) + `SubtitleHolobox`, `VoiceModule` (Vosk, PTT, `FarFieldAudioConditioner`, `OwnerVoiceprintGuard`, Escudo, Edge-TTS, Ducking). | Detección de wake-word con música sonando sin falsos disparos. Audio sintetizado sincronizado con orbe y ducking en vivo. |
| **5** | **Inteligencia Web (0 Claves) y Visión** | `IntelModule` (Gemini Search/DuckDuckGo, links en navegador, Open-Meteo, RSS, Google Docs), `VisionModule` (captura DPI-aware, Guía Live por turnos). | Consulta de clima/noticias en vivo, apertura de link en el navegador del PC y sesión de Guía Live por turnos respondiendo a la voz. |
| **6** | **Portal Web GitHub Pages + OAuth Base** | Portal estático (`index.html`, `guides.html`, `privacy.html`, `terms.html`, `callback.html`), servidor callback `127.0.0.1:8888`, Setup Sync directo. | Portal navegado en GitHub Pages; flujo callback OAuth y envío directo de configuración al DeusEx local funcionando. |
| **7** | **Música Universal + Google Workspace** | `MusicModule` (SMTC, Spotify PKCE, TIDAL PKCE, YT Music, Apple Music, mutagen), `WorkspaceModule` (Gmail, Calendar, Tasks, Drive, Google Docs). | Iniciar sesión 1-clic en Google/Spotify/TIDAL. Creación real de evento en Calendar, doc en Google Docs y control de reproducción en Spotify/SMTC. |
| **8** | **Discord privadas + WhatsApp Baileys** | `DiscordGatewayService` (`discord_chat` control/avisos; `discord_music` DAVE E2EE, RMS NumPy, `!play/!shuffle`), `WhatsAppModule` (Baileys QR, resumen audios). | Bot de Discord reproduce audio en canal de voz con comando `!shuffle` bajo demanda. WhatsApp se vincula por QR y resume una nota de voz real. |
| **9** | **Pulido Comercial, Doctor, Bridge WS e Instalador** | Diagnostic `Doctor`, Export/Import `.zip`, visor logs, chequeo GitHub Releases, `bridge/ws_server.py` QR, build `PyInstaller` + Inno Setup. | Instalación de `DeusEx.exe` en Windows 11. Doctor en verde, respaldo `.zip` sin secretos y servidor WebSocket respondiendo en LAN. |
| **10** | **App Móvil Companion (.apk) + Smartwatch** | App Android nativa (Kotlin + Jetpack Compose, cero ADB), emparejamiento QR, control remoto, receptora de capturas, Mini-DeusEx local, Health Connect. | `.apk` instalada en smartphone escanea el QR del PC, recibe capturas de pantalla, aprueba acciones `SafetyGate` y funciona en local fuera de casa. |

---
*Este documento constituye la especificación final de producto para DeusEx v3.*