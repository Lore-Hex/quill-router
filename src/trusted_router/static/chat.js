"use strict";
/* TrustedRouter chat playground at /chat — chunk 2: single-model
 * working end-to-end with SSE streaming.
 *
 * Chunk 3 adds multi-model parallel send + side-by-side columns.
 * Chunk 4 is polish (attachments, voice, shortcuts, export, share).
 *
 * Design contracts:
 *   * Vanilla JS, no framework. State lives in localStorage so the
 *     "0 prompt logs" promise from the homepage holds — TR servers
 *     never see the conversation.
 *   * Send button gated client-side on hasSignedInHint() from
 *     dashboard.js. Signed-out clicks pop the existing #signinModal
 *     and fire ZERO requests to api.trustedrouter.com.
 *   * Browser-side API key auto-issued via
 *     POST /internal/chat/issue-browser-key on first signed-in Send.
 *     Server returns the raw key in a one-shot tr_chat_key cookie;
 *     we copy it to sessionStorage and clear the cookie.
 *   * SSE streaming via fetch + getReader. Standard OpenAI delta
 *     protocol — `data: {json}\n\n` chunks, `data: [DONE]` terminator.
 *   * Markdown rendered with marked + highlight.js, sanitized with
 *     DOMPurify before insertion. Model-generated <script> tags
 *     CANNOT execute.
 */

(function () {
    // ── Constants ─────────────────────────────────────────────────────
    const CHAT_CONFIG = window.__TR_CHAT__ || {};
    const LOCKED_MODEL_ID = (CHAT_CONFIG.lockedModelId || "").trim();
    const LOCKED_MODEL_LABEL = (CHAT_CONFIG.lockedModelLabel || "Custom model").trim();
    const STORAGE_KEY = CHAT_CONFIG.storageKey || "tr_chat_state_v1";
    const KEY_SESSION_STORAGE = "tr_chat_key";
    const KEY_COOKIE =
        CHAT_CONFIG.keyCookieName || "tr_chat_key";
    // Inference endpoints (chat/completions, messages, responses) go
    // through the same-origin chat-proxy because direct cross-origin
    // fetch to api.trustedrouter.com is CORS-blocked by the attested
    // gateway. Server-side template renders this as "/chat-proxy/v1".
    const API_BASE =
        CHAT_CONFIG.apiBaseUrl ||
        "/chat-proxy/v1";
    // Public catalog endpoint — TR control plane serves /v1/models
    // anonymously, so the picker can load without any browser key /
    // proxy hop. Avoids hitting the attested gateway's 401 for an
    // unauthenticated catalog list.
    const CATALOG_BASE =
        CHAT_CONFIG.catalogBaseUrl ||
        "/v1";
    const ISSUE_KEY_PATH =
        CHAT_CONFIG.issueKeyPath ||
        "/internal/chat/issue-browser-key";
    const DEFAULT_MODEL_ID = LOCKED_MODEL_ID || "anthropic/claude-sonnet-4.6";
    const MAX_MODELS_PER_CHAT = 4; // matches OpenRouter's apparent cap

    // Curated "Popular" list surfaced at the top of the picker when the
    // user hasn't typed anything yet. Mirrors DEFAULT_AUTO_MODEL_ORDER
    // in catalog.py plus the community-favourite OSS models so first
    // open isn't an empty search box. Order is intentional: best
    // headline pick first, then variety across providers + price tiers.
    const POPULAR_MODEL_IDS = [
        "anthropic/claude-sonnet-4.6",
        "openai/gpt-5.4-nano",
        "moonshotai/kimi-k2.6",
        "deepseek/deepseek-v4-flash",
        "google/gemini-2.5-flash",
        "google/gemma-4-31b-it",
        "z-ai/glm-4.6",
        "anthropic/claude-opus-4.7",
        "mistralai/mistral-small-2603",
        "meta-llama/llama-3.3-70b-instruct",
    ];
    // Full param set OpenRouter exposes. Per-model overrides; passed
    // through to /v1/chat/completions as-is. Providers that don't
    // recognize a param (e.g. OpenAI doesn't have top_k) simply
    // ignore it.
    const DEFAULT_PARAMS = {
        temperature: 1.0,
        top_p: 1.0,
        top_k: 0, // 0 = disabled / provider default
        max_tokens: 1024,
        frequency_penalty: 0,
        presence_penalty: 0,
        repetition_penalty: 1.0,
        min_p: 0,
        top_a: 0,
    };
    // Which params to include in the API request body. We omit params
    // that match the provider-neutral default (0/1.0/disabled) so the
    // wire payload doesn't push every provider into explicit-set mode.
    function buildParamsForRequest(slot) {
        const out = {};
        for (const k of Object.keys(slot.params)) {
            const v = slot.params[k];
            const def = DEFAULT_PARAMS[k];
            if (v === def) continue;
            out[k] = v;
        }
        return out;
    }

    // ── Cost formatting ───────────────────────────────────────────────
    // Cost is stored as microdollars (1 USD = 1_000_000 μ$). At the
    // small per-message scale we routinely show $0.0000 which reads as
    // "free" even when it's not. Adapt the unit:
    //   * exactly 0                  → "$0"
    //   * (0, 1000μ$]   <0.1¢        → "<0.1¢"
    //   * (1000, 10⁶μ$] in cents     → "X.XX¢"
    //   * ≥ 10⁶μ$ ($1)  in dollars   → "$X.XX" / "$XX.XX"
    function formatCost(microdollars, opts) {
        const o = opts || {};
        const prefix = o.estimate ? "≈" : "";
        if (microdollars == null || microdollars === 0) {
            return prefix + "$0";
        }
        if (microdollars < 0) microdollars = 0;
        // Sub-tenth-cent territory — too small to show usefully.
        if (microdollars < 1000) return prefix + "<0.1¢";
        // Cents range: 0.1¢ – 99.9¢
        if (microdollars < 1_000_000) {
            const cents = microdollars / 10_000;
            const digits = cents < 1 ? 2 : cents < 10 ? 2 : 1;
            return prefix + cents.toFixed(digits) + "¢";
        }
        // Dollars
        const dollars = microdollars / 1_000_000;
        const digits = dollars < 10 ? 2 : dollars < 100 ? 2 : 0;
        return prefix + "$" + dollars.toFixed(digits);
    }

    // ── State ─────────────────────────────────────────────────────────
    /** @type {{chats: Object, activeChatId: string|null, preferences: Object}} */
    let STATE = loadState();
    /** @type {Array<Object>} cached model catalog from /v1/models */
    let MODELS = [];
    /** @type {boolean} */
    let MODELS_LOADING = false;
    /** @type {Map<string, AbortController>} active stream cancellation handles */
    const STREAMS = new Map();

    function loadState() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (raw) {
                const parsed = JSON.parse(raw);
                if (parsed && typeof parsed === "object") {
                    parsed.chats = parsed.chats && typeof parsed.chats === "object"
                        ? parsed.chats
                        : {};
                    parsed.preferences = parsed.preferences && typeof parsed.preferences === "object"
                        ? parsed.preferences
                        : {};
                    // Walk every chat and heal missing/wrong-typed fields so a
                    // schema drift between deploys can't crash later
                    // renderers. Drops chats that are completely
                    // unrecoverable (non-object).
                    const healedChats = {};
                    for (const [id, chat] of Object.entries(parsed.chats)) {
                        const healed = healChatShape(id, chat);
                        if (healed) healedChats[healed.id] = healed;
                    }
                    parsed.chats = healedChats;
                    // If the previously-active chat got dropped during
                    // heal, fall back to the most recent remaining one
                    // (or null, in which case ensureActiveChat creates
                    // a fresh chat).
                    if (
                        !parsed.activeChatId ||
                        !healedChats[parsed.activeChatId]
                    ) {
                        const ids = Object.keys(healedChats);
                        parsed.activeChatId = ids.length > 0 ? ids[0] : null;
                    }
                    return parsed;
                }
            }
        } catch (_) {
            // Corrupt state — reset rather than break the page
        }
        return { chats: {}, activeChatId: null, preferences: {} };
    }

    // Belt-and-suspenders chat-shape healer. Returns a guaranteed-valid
    // chat object or null if the input is unrecoverable. Every renderer
    // assumes these fields are present and the right type; without this
    // a stale chat from an old schema (or one written by a bad partial
    // save) crashes the page on load.
    function healChatShape(id, chat) {
        if (!chat || typeof chat !== "object") return null;
        const out = {
            id: typeof chat.id === "string" ? chat.id : (id || newChatId()),
            title: typeof chat.title === "string" ? chat.title : "New chat",
            created_at: typeof chat.created_at === "string" ? chat.created_at : isoNow(),
            updated_at: typeof chat.updated_at === "string" ? chat.updated_at : isoNow(),
            shared_system_prompt: typeof chat.shared_system_prompt === "string"
                ? chat.shared_system_prompt
                : "",
            pinned: !!chat.pinned,
            messages: Array.isArray(chat.messages) ? chat.messages : [],
            models: Array.isArray(chat.models) && chat.models.length > 0
                ? chat.models.map(_healSlotShape).filter(Boolean)
                : [_defaultSlot()],
        };
        // If healing the models list killed every slot, fall back to one
        // default model so the chat is still usable.
        if (out.models.length === 0) out.models = [_defaultSlot()];
        return out;
    }

    function _healSlotShape(slot) {
        if (!slot || typeof slot !== "object") return null;
        return {
            model_id: typeof slot.model_id === "string" && slot.model_id
                ? slot.model_id
                : DEFAULT_MODEL_ID,
            system_prompt: typeof slot.system_prompt === "string" ? slot.system_prompt : "",
            params: slot.params && typeof slot.params === "object"
                ? { ...DEFAULT_PARAMS, ...slot.params }
                : { ...DEFAULT_PARAMS },
            enabled: slot.enabled !== false,
            label: typeof slot.label === "string" ? slot.label : "",
            provider_preferences: slot.provider_preferences && typeof slot.provider_preferences === "object"
                ? slot.provider_preferences
                : undefined,
        };
    }

    function _defaultSlot() {
        // Note: can't read from STATE here — _defaultSlot is called
        // FROM inside loadState, which runs before STATE is assigned
        // (TDZ for the `let STATE = loadState()` line). DEFAULT_MODEL_ID
        // is the safest fallback.
        return {
            model_id: DEFAULT_MODEL_ID,
            system_prompt: "",
            params: { ...DEFAULT_PARAMS },
            enabled: true,
            label: "",
        };
    }

    function saveState() {
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(STATE));
        } catch (e) {
            // localStorage may be over-quota or unavailable. Fail silent
            // — the chat continues working in-memory for this session.
            console.warn("chat: localStorage save failed:", e);
        }
    }

    function newChatId() {
        return "c_" + Math.random().toString(36).slice(2, 12);
    }

    function newMsgId() {
        return "m_" + Math.random().toString(36).slice(2, 12);
    }

    function isoNow() {
        return new Date().toISOString();
    }

    // ── Auth + key acquisition ────────────────────────────────────────

    function isSignedIn() {
        if (typeof window.hasSignedInHint === "function") {
            return window.hasSignedInHint();
        }
        return document.cookie
            .split(";")
            .map((c) => c.trim())
            .some((c) => c === "tr_signed_in=1");
    }

    function openSigninModal() {
        if (typeof window.openSigninModal === "function") {
            window.openSigninModal();
            return;
        }
        const dialog = document.getElementById("signinModal");
        if (dialog && typeof dialog.showModal === "function" && !dialog.open) {
            dialog.showModal();
        }
    }

    function bootstrapBrowserKey() {
        const match = document.cookie
            .split(";")
            .map((c) => c.trim())
            .find((c) => c.startsWith(KEY_COOKIE + "="));
        if (!match) return;
        const raw = decodeURIComponent(match.slice(KEY_COOKIE.length + 1));
        try {
            sessionStorage.setItem(KEY_SESSION_STORAGE, raw);
        } catch (_) {}
        // Clear cookie immediately (one-shot pattern).
        document.cookie =
            KEY_COOKIE + "=; path=/chat; expires=Thu, 01 Jan 1970 00:00:00 GMT";
    }

    /** Returns the raw browser-API key. Fetches a new one if missing. */
    async function ensureBrowserKey(opts) {
        const forceRefresh = !!(opts && opts.forceRefresh);
        let key = null;
        if (!forceRefresh) {
            try {
                key = sessionStorage.getItem(KEY_SESSION_STORAGE);
            } catch (_) {}
            if (key) return key;
        } else {
            // Caller hit a 401 — the cached key is stale. Drop it before
            // asking for a fresh one so we don't accidentally reuse it
            // on the next call.
            try { sessionStorage.removeItem(KEY_SESSION_STORAGE); } catch (_) {}
        }

        // POST to the issue-key endpoint. Same-origin (trustedrouter.com)
        // so the session cookie is sent automatically.
        const resp = await fetch(ISSUE_KEY_PATH, {
            method: "POST",
            credentials: "same-origin",
        });
        if (resp.status === 302 || resp.status === 401) {
            // Server-side gate said "not signed in" — JS shouldn't have
            // called us. Pop the modal and bail.
            openSigninModal();
            throw new Error("not signed in");
        }
        if (!resp.ok) {
            throw new Error("issue-key failed: " + resp.status);
        }
        // Body has the raw key. Cookie was also set; clear it
        // immediately for the one-shot guarantee even though we got
        // the value from the body.
        const json = await resp.json();
        const raw = (json && json.data && json.data.raw_key) || null;
        if (!raw) throw new Error("issue-key returned no raw_key");
        try {
            sessionStorage.setItem(KEY_SESSION_STORAGE, raw);
        } catch (_) {}
        document.cookie =
            KEY_COOKIE + "=; path=/chat; expires=Thu, 01 Jan 1970 00:00:00 GMT";
        return raw;
    }

    // ── Model catalog ─────────────────────────────────────────────────

    async function loadModels() {
        if (MODELS_LOADING || MODELS.length > 0) return;
        MODELS_LOADING = true;
        try {
            if (window.TrustedRouterModelCatalog) {
                MODELS = await window.TrustedRouterModelCatalog.loadModels(CATALOG_BASE);
            } else {
                const resp = await fetch(CATALOG_BASE + "/models");
                if (!resp.ok) throw new Error("models fetch " + resp.status);
                const json = await resp.json();
                const data = Array.isArray(json.data) ? json.data : [];
                // Each model has top-level id + an OpenRouter-shaped
                // pricing block; TR also surfaces a `trustedrouter`
                // extension with provider-specific context.
                MODELS = data.map((m) => normalizeModel(m));
            }
            if (LOCKED_MODEL_ID && !MODELS.some((m) => m.id === LOCKED_MODEL_ID)) {
                MODELS.push({
                    id: LOCKED_MODEL_ID,
                    name: LOCKED_MODEL_LABEL || LOCKED_MODEL_ID,
                    provider: "trustedrouter",
                    capabilities: [],
                    free: false,
                    internal_only: false,
                });
            }
            renderModelPicker();
        } catch (e) {
            console.warn("chat: model catalog load failed:", e);
        } finally {
            MODELS_LOADING = false;
        }
    }

    function normalizeModel(raw) {
        if (window.TrustedRouterModelCatalog) {
            return window.TrustedRouterModelCatalog.normalizeModel(raw);
        }
        const pricing = raw.pricing || {};
        const ext = raw.trustedrouter || {};
        // Pricing in OpenAI shape is dollars per token; convert to
        // $/M for display.
        const inPerM =
            pricing.prompt != null ? Number(pricing.prompt) * 1_000_000 : null;
        const outPerM =
            pricing.completion != null
                ? Number(pricing.completion) * 1_000_000
                : null;
        // Catalog rarely populates `capabilities` so the picker's
        // vision/tools filters were dead chips. Derive flags from the
        // model id when the catalog doesn't carry them — keeps the
        // filters useful immediately, and the catalog-supplied list
        // takes precedence if it's there.
        const catalogCaps = ext.capabilities || [];
        const inferredCaps = inferCapabilities(raw.id || "");
        const allCaps = Array.from(
            new Set([...catalogCaps, ...inferredCaps]),
        );
        return {
            id: raw.id,
            name: raw.name || raw.id,
            description: raw.description || "",
            context_length: raw.context_length || ext.context_length || null,
            input_per_m: inPerM,
            output_per_m: outPerM,
            uptime_pct: ext.uptime_pct || null,
            capabilities: allCaps,
            free: pricing && Number(pricing.prompt) === 0,
            total_per_m: (inPerM || 0) + (outPerM || 0),
            // Internal-only routing pools (trustedrouter/monitor) leak
            // through some catalog snapshots; track the flag so the
            // picker can drop them defensively.
            internal_only: !!ext.internal_only,
        };
    }

    // Heuristic capability detection from model id — used when the
    // catalog doesn't populate the `capabilities` field. Captures the
    // model families that publicly advertise vision (image input) and
    // tool-use; anything else falls through and the chip filter just
    // won't match it. Picky over generous so we don't promise
    // capabilities a model doesn't have.
    function inferCapabilities(id) {
        if (window.TrustedRouterModelCatalog) {
            return window.TrustedRouterModelCatalog.inferCapabilities(id);
        }
        const i = (id || "").toLowerCase();
        const caps = [];
        // Vision-capable families (image input via /chat/completions
        // multimodal parts):
        const VISION_FAMILIES = [
            "claude-opus", "claude-sonnet", "claude-haiku",
            "gpt-5", "gpt-4o", "gpt-4-vision", "gpt-4-turbo",
            "gemini-2", "gemini-1.5", "gemini-pro-vision",
            "llama-3.2-vision", "llama-3.3-vision",
            "qwen-vl", "qwen2-vl", "qwen2.5-vl",
            "pixtral", "molmo", "internvl", "minicpm-v",
            "minimax-m2", "minimax-m2.1", "minimax-m2.5", "minimax-m2.7",
            "step-1v", "yi-vision", "phi-3.5-vision",
            "vision", // any explicit "-vision" suffix
        ];
        if (VISION_FAMILIES.some((f) => i.includes(f))) caps.push("vision");
        // Tool-use families (function calling reliably supported):
        const TOOLS_FAMILIES = [
            "claude-opus", "claude-sonnet", "claude-haiku",
            "gpt-5", "gpt-4o", "gpt-4-turbo", "gpt-4.1", "gpt-4.5",
            "gemini-2", "gemini-1.5",
            "mistral-large", "mistral-small", "mistral-medium",
            "llama-3.1-70b-instruct", "llama-3.3-70b-instruct",
            "qwen2.5", "qwen3",
            "deepseek-v3", "deepseek-v4", "deepseek-r1",
            "kimi-k2", "glm-4", "yi-large",
            "command-r", "nova-pro", "nova-lite",
        ];
        if (TOOLS_FAMILIES.some((f) => i.includes(f))) caps.push("tools");
        return caps;
    }

    function findModel(id) {
        return MODELS.find((m) => m.id === id) || null;
    }

    // ── Active chat helpers ───────────────────────────────────────────

    function getActiveChat() {
        if (!STATE.activeChatId) return null;
        return STATE.chats[STATE.activeChatId] || null;
    }

    function newChat() {
        const chat = {
            id: newChatId(),
            title: LOCKED_MODEL_ID ? LOCKED_MODEL_LABEL : "New chat",
            created_at: isoNow(),
            updated_at: isoNow(),
            models: [
                {
                    model_id:
                        STATE.preferences.lastModelId || DEFAULT_MODEL_ID,
                    system_prompt: "",
                    params: { ...DEFAULT_PARAMS },
                    enabled: true,
                },
            ],
            shared_system_prompt: STATE.preferences.defaultSystemPrompt || "",
            messages: [],
        };
        STATE.chats[chat.id] = chat;
        STATE.activeChatId = chat.id;
        saveState();
        renderSidebar();
        renderModelsBar();
        renderThread();
        renderSystemPrompt();
        return chat;
    }

    function ensureActiveChat() {
        let chat = getActiveChat();
        if (!chat) chat = newChat();
        return chat;
    }

    function deleteChat(chatId) {
        delete STATE.chats[chatId];
        if (STATE.activeChatId === chatId) {
            const ids = Object.keys(STATE.chats);
            STATE.activeChatId = ids.length > 0 ? ids[0] : null;
        }
        saveState();
        renderSidebar();
        renderModelsBar();
        renderThread();
        renderSystemPrompt();
    }

    function clearCurrentChat() {
        const chat = ensureActiveChat();
        if (!chat.messages.length && !(chat._pending_attachments || []).length) {
            showToast("Current chat is already empty");
            return;
        }
        confirmModal({
            title: "Clear current chat?",
            message: "Messages and pending attachments in this chat will be removed. Model settings stay the same.",
            confirmText: "Clear chat",
            danger: true,
        }).then((ok) => {
            if (!ok) return;
            chat.messages = [];
            chat._pending_attachments = [];
            chat.title = "New chat";
            chat.updated_at = isoNow();
            saveState();
            renderSidebar();
            renderModelsBar();
            renderThread();
            renderAttachmentTray();
            updateInputEstimate();
            showToast("Chat cleared");
        });
    }

    function setActiveChat(chatId) {
        STATE.activeChatId = chatId;
        saveState();
        renderSidebar();
        renderModelsBar();
        renderThread();
        renderSystemPrompt();
    }

    function updateChatTitle(chat, firstUserMessage) {
        if (chat.title === "New chat" && firstUserMessage) {
            // First ~40 chars of the user's first message, single line.
            const t = firstUserMessage.replace(/\s+/g, " ").trim().slice(0, 40);
            chat.title = t || "New chat";
        }
    }

    // ── Render: sidebar (chat list grouped by date) ───────────────────

    function dateBucket(iso) {
        const d = new Date(iso);
        const now = new Date();
        const sameDay =
            d.getFullYear() === now.getFullYear() &&
            d.getMonth() === now.getMonth() &&
            d.getDate() === now.getDate();
        if (sameDay) return "TODAY";
        const y = new Date(now);
        y.setDate(now.getDate() - 1);
        const isYesterday =
            d.getFullYear() === y.getFullYear() &&
            d.getMonth() === y.getMonth() &&
            d.getDate() === y.getDate();
        if (isYesterday) return "YESTERDAY";
        return "OLDER";
    }

    let SIDEBAR_QUERY = "";

    function renderSidebar() {
        const list = document.querySelector("[data-chat-list]");
        if (!list) return;
        list.innerHTML = "";
        const q = SIDEBAR_QUERY.trim().toLowerCase();
        let chats = Object.values(STATE.chats);
        // Pinned chats first (independent of date sort), then by recency.
        chats.sort((a, b) => {
            const pa = a.pinned ? 1 : 0;
            const pb = b.pinned ? 1 : 0;
            if (pa !== pb) return pb - pa;
            return new Date(b.updated_at) - new Date(a.updated_at);
        });
        if (q) {
            chats = chats.filter((c) => {
                if ((c.title || "").toLowerCase().includes(q)) return true;
                // Also match against the most recent user message so a
                // searcher who remembers what they asked finds the chat.
                const lastUser = (c.messages || [])
                    .filter((m) => m.role === "user")
                    .slice(-1)[0];
                return (
                    lastUser &&
                    (lastUser.content || "").toLowerCase().includes(q)
                );
            });
        }
        if (chats.length === 0) {
            const empty = document.createElement("div");
            empty.className = "chat-sidebar-empty";
            empty.textContent = q ? "No matches." : "No chats yet.";
            list.appendChild(empty);
            return;
        }
        let lastBucket = "";
        for (const chat of chats) {
            // Pinned items get their own bucket label at the top.
            const bucket = chat.pinned ? "PINNED" : dateBucket(chat.updated_at);
            if (bucket !== lastBucket) {
                const header = document.createElement("div");
                header.className = "chat-sidebar-bucket";
                header.textContent = bucket;
                list.appendChild(header);
                lastBucket = bucket;
            }
            const item = document.createElement("div");
            item.className = "chat-sidebar-item";
            item.dataset.chatId = chat.id;
            if (chat.id === STATE.activeChatId) item.classList.add("is-active");
            const title = document.createElement("button");
            title.type = "button";
            title.className = "chat-sidebar-title";
            const chatCostMicro = sumChatCostMicrodollars(chat);
            const costSpan = chatCostMicro > 0
                ? '<span class="chat-sidebar-title-cost" title="Total spent on this chat">' +
                  escapeHtml(formatCost(chatCostMicro)) +
                  "</span>"
                : "";
            title.innerHTML =
                '<span class="chat-sidebar-title-text">' +
                escapeHtml(chat.title) +
                "</span>" +
                '<span class="chat-sidebar-title-meta">' +
                '<span class="chat-sidebar-title-time">' +
                escapeHtml(relativeTime(chat.updated_at)) +
                "</span>" +
                costSpan +
                "</span>";
            title.addEventListener("click", () => setActiveChat(chat.id));
            const pin = document.createElement("button");
            pin.type = "button";
            pin.className = "chat-sidebar-pin";
            if (chat.pinned) pin.classList.add("is-pinned");
            pin.setAttribute(
                "aria-label",
                chat.pinned ? "Unpin chat" : "Pin chat",
            );
            pin.textContent = chat.pinned ? "★" : "☆";
            pin.addEventListener("click", (e) => {
                e.stopPropagation();
                chat.pinned = !chat.pinned;
                chat.updated_at = isoNow();
                saveState();
                renderSidebar();
            });
            const del = document.createElement("button");
            del.type = "button";
            del.className = "chat-sidebar-delete";
            del.setAttribute("aria-label", "Delete chat");
            del.textContent = "×";
            del.addEventListener("click", (e) => {
                e.stopPropagation();
                confirmModal({
                    title: "Delete this chat?",
                    message: "This chat and all its messages will be removed from this browser.",
                    confirmText: "Delete",
                    danger: true,
                }).then((ok) => {
                    if (ok) deleteChat(chat.id);
                });
            });
            item.appendChild(title);
            item.appendChild(pin);
            item.appendChild(del);
            list.appendChild(item);
        }
    }

    // ── Render: models bar (chunk 2 = 1 pill; chunk 3 = up to 4) ─────

    // Tracks which slot's settings dropdown (if any) is open.
    let openDropdownSlotIdx = -1;

    function renderModelsBar() {
        const bar = document.querySelector("[data-chat-models-bar]");
        if (!bar) return;
        bar.innerHTML = "";
        const chat = ensureActiveChat();
        if (LOCKED_MODEL_ID) {
            chat.models = [{
                model_id: LOCKED_MODEL_ID,
                system_prompt: "",
                params: chat.models[0] ? { ...DEFAULT_PARAMS, ...chat.models[0].params } : { ...DEFAULT_PARAMS },
                enabled: true,
                label: LOCKED_MODEL_LABEL,
            }];
        }

        chat.models.forEach((slot, idx) => {
            bar.appendChild(makeModelPill(chat, slot, idx));
        });

        if (!LOCKED_MODEL_ID && chat.models.length < MAX_MODELS_PER_CHAT) {
            const addBtn = document.createElement("button");
            addBtn.type = "button";
            addBtn.className = "chat-model-add";
            addBtn.dataset.action = "add-model";
            addBtn.setAttribute("aria-label", "Add another model to compare");
            addBtn.textContent = "+ Add model";
            bar.appendChild(addBtn);
        }
        renderHeaderMeta();
    }

    function renderHeaderMeta() {
        const titleEl = document.querySelector("[data-chat-header-title]");
        const costEl = document.querySelector("[data-chat-header-cost]");
        const chat = getActiveChat();
        if (titleEl) {
            titleEl.textContent = (chat && chat.title) || "";
        }
        updateTabTitle();
        if (costEl) {
            if (!chat) {
                costEl.textContent = "";
                return;
            }
            let totalMicro = 0;
            let totalIn = 0;
            let totalOut = 0;
            for (const m of chat.messages || []) {
                if (m.role !== "assistant") continue;
                for (const r of m.responses || []) {
                    totalMicro += r.cost_microdollars || 0;
                    totalIn += r.tokens_in || 0;
                    totalOut += r.tokens_out || 0;
                }
            }
            if (totalMicro === 0 && totalIn === 0 && totalOut === 0) {
                costEl.textContent = "";
            } else {
                costEl.textContent =
                    formatCost(totalMicro) +
                    "  ·  " + totalIn + " in / " + totalOut + " out";
            }
        }
    }

    function makeModelPill(chat, slot, idx) {
        const wrap = document.createElement("div");
        wrap.className = "chat-model-pill-wrap";
        if (!slot.enabled) wrap.classList.add("is-disabled");
        const model = findModel(slot.model_id);
        const pill = document.createElement("button");
        pill.type = "button";
        pill.className = "chat-model-pill";
        if (LOCKED_MODEL_ID) pill.title = LOCKED_MODEL_ID;
        if (!LOCKED_MODEL_ID) pill.dataset.action = "toggle-model-dropdown";
        pill.dataset.slotIdx = String(idx);
        const label =
            slot.label ||
            (model && model.name) ||
            slot.model_id ||
            "Select a model";
        const provider = providerFromModelId(slot.model_id);
        const avatar = providerAvatar(provider, "chat-avatar-pill");
        // Provider chip is visible on desktop (>=600px viewport); CSS
        // hides it on mobile where horizontal space is tight. The
        // avatar still carries the provider identity at smaller sizes.
        const providerChip = provider
            ? '<span class="chat-model-pill-provider" title="Provider: ' +
              escapeHtml(provider) +
              '">' + escapeHtml(provider) + "</span>"
            : "";
        pill.innerHTML =
            avatar +
            '<span class="chat-model-pill-name">' +
            escapeHtml(label) +
            "</span>" +
            providerChip +
            (chat.models.length > 1
                ? '<span class="chat-model-pill-num">#' + (idx + 1) + "</span>"
                : "") +
            (LOCKED_MODEL_ID ? "" : '<span class="chat-model-pill-caret">▾</span>');
        wrap.appendChild(pill);
        // Inline × close button — OR-style one-click model removal.
        // Visible on hover; on mobile (no hover) it's always visible.
        // For the LAST model the action becomes "reset to default"
        // rather than "remove" so the user is never stuck with zero.
        if (!LOCKED_MODEL_ID) {
            const closer = document.createElement("button");
            closer.type = "button";
            closer.className = "chat-model-pill-close";
            closer.dataset.action = "remove-model";
            closer.dataset.slotIdx = String(idx);
            closer.title = chat.models.length > 1
                ? "Remove this model from the chat"
                : "Reset to default model";
            closer.setAttribute("aria-label", closer.title);
            closer.innerHTML = "×";
            wrap.appendChild(closer);
        }
        if (openDropdownSlotIdx === idx) {
            wrap.appendChild(makeModelDropdown(chat, slot, idx));
        }
        return wrap;
    }

    function makeModelDropdown(chat, slot, idx) {
        const dd = document.createElement("div");
        dd.className = "chat-model-dropdown";
        const enabledChecked = slot.enabled ? "checked" : "";
        const sliderRow = (key, min, max, step, label) => {
            const v = slot.params[key];
            const display =
                step >= 1 ? String(v) : Number(v).toFixed(step < 0.1 ? 2 : 1);
            return (
                '<label class="chat-dd-slider">' +
                '<span class="chat-dd-slider-label">' +
                label +
                "</span>" +
                '<input type="range" min="' +
                min +
                '" max="' +
                max +
                '" step="' +
                step +
                '" value="' +
                v +
                '" data-param="' +
                key +
                '" data-slot-idx="' +
                idx +
                '">' +
                '<span class="chat-dd-slider-value">' +
                display +
                "</span>" +
                "</label>"
            );
        };
        dd.innerHTML =
            '<div class="chat-dd-row chat-dd-row-flex">' +
            '<button type="button" class="chat-dd-action" data-action="open-model-picker" data-slot-idx="' +
            idx +
            '">Change model</button>' +
            '<button type="button" class="chat-dd-action" data-action="duplicate-model" data-slot-idx="' +
            idx +
            '">Duplicate</button>' +
            // Always allow Remove. When it's the last model, removeModel()
            // resets the slot to the default model instead of leaving the
            // chat empty — matches OR's "you always have at least one
            // model" invariant without the user feeling stuck.
            '<button type="button" class="chat-dd-action chat-dd-action-danger" data-action="remove-model" data-slot-idx="' +
            idx +
            '">' + (chat.models.length > 1 ? "Remove" : "Reset") + "</button>" +
            "</div>" +
            '<div class="chat-dd-row">' +
            '<label class="chat-dd-toggle"><input type="checkbox" ' +
            enabledChecked +
            ' data-action="toggle-enabled" data-slot-idx="' +
            idx +
            '">Enabled</label>' +
            '<input type="text" class="chat-dd-label-input" placeholder="Rename (optional)" value="' +
            escapeHtml(slot.label || "") +
            '" data-action="rename-model" data-slot-idx="' +
            idx +
            '">' +
            "</div>" +
            '<div class="chat-dd-row chat-dd-sys-row">' +
            '<label class="chat-dd-sys-label">System prompt override' +
            '<textarea data-action="override-sys" data-slot-idx="' +
            idx +
            '" rows="2" placeholder="Falls back to chat-level if empty.">' +
            escapeHtml(slot.system_prompt || "") +
            "</textarea></label>" +
            "</div>" +
            '<div class="chat-dd-row">' +
            '<label class="chat-dd-sys-label">Routing</label>' +
            '<select class="chat-dd-routing-select" data-action="set-routing-sort" data-slot-idx="' +
            idx +
            '">' +
            '<option value="">Auto (TR picks best)</option>' +
            '<option value="latency"' +
            (slot.provider_preferences &&
            slot.provider_preferences.sort_by === "latency"
                ? " selected"
                : "") +
            ">Fastest provider</option>" +
            '<option value="cost"' +
            (slot.provider_preferences &&
            slot.provider_preferences.sort_by === "cost"
                ? " selected"
                : "") +
            ">Cheapest provider</option>" +
            '<option value="uptime"' +
            (slot.provider_preferences &&
            slot.provider_preferences.sort_by === "uptime"
                ? " selected"
                : "") +
            ">Most reliable provider</option>" +
            "</select>" +
            "</div>" +
            '<div class="chat-dd-row chat-dd-presets-row">' +
            '<label class="chat-dd-sys-label">Presets</label>' +
            '<div class="chat-dd-presets">' +
            getPresets()
                .map(
                    (p) =>
                        '<button type="button" class="chat-dd-preset" data-action="load-preset" data-slot-idx="' +
                        idx +
                        '" data-preset-name="' +
                        escapeHtml(p.name) +
                        '">' +
                        escapeHtml(p.name) +
                        "</button>",
                )
                .join("") +
            '<button type="button" class="chat-dd-action" data-action="save-preset" data-slot-idx="' +
            idx +
            '">+ Save current</button>' +
            "</div>" +
            "</div>" +
            '<div class="chat-dd-sliders">' +
            sliderRow("temperature", 0, 2, 0.1, "Temperature") +
            sliderRow("top_p", 0, 1, 0.05, "Top P") +
            sliderRow("top_k", 0, 100, 1, "Top K") +
            sliderRow("max_tokens", 32, 8192, 32, "Max tokens") +
            sliderRow("frequency_penalty", -2, 2, 0.1, "Frequency penalty") +
            sliderRow("presence_penalty", -2, 2, 0.1, "Presence penalty") +
            sliderRow("repetition_penalty", 0.5, 2, 0.05, "Repetition penalty") +
            sliderRow("min_p", 0, 1, 0.01, "Min P") +
            sliderRow("top_a", 0, 1, 0.01, "Top A") +
            "</div>";

        dd.addEventListener("input", (e) => {
            const target = e.target;
            if (!target || !target.dataset) return;
            const slotIdx = parseInt(target.dataset.slotIdx, 10);
            const which = target.dataset.param;
            const action = target.dataset.action;
            const targetSlot = chat.models[slotIdx];
            if (!targetSlot) return;
            if (which) {
                const val =
                    target.step && parseFloat(target.step) >= 1
                        ? parseInt(target.value, 10)
                        : parseFloat(target.value);
                targetSlot.params[which] = val;
                const valSpan = target.nextElementSibling;
                if (valSpan)
                    valSpan.textContent =
                        target.step && parseFloat(target.step) >= 1
                            ? String(val)
                            : val.toFixed(target.step < 0.1 ? 2 : 1);
            } else if (action === "override-sys") {
                targetSlot.system_prompt = target.value;
                updateSysButtonState();
            } else if (action === "rename-model") {
                targetSlot.label = target.value;
                renderModelsBar();
            }
            chat.updated_at = isoNow();
            saveState();
        });

        dd.addEventListener("change", (e) => {
            const target = e.target;
            if (!target) return;
            const slotIdx = parseInt(target.dataset.slotIdx, 10);
            const targetSlot = chat.models[slotIdx];
            if (!targetSlot) return;
            if (target.dataset.action === "toggle-enabled") {
                targetSlot.enabled = target.checked;
                chat.updated_at = isoNow();
                saveState();
                renderModelsBar();
                return;
            }
            if (target.dataset.action === "set-routing-sort") {
                if (!targetSlot.provider_preferences) {
                    targetSlot.provider_preferences = {};
                }
                if (target.value) {
                    targetSlot.provider_preferences.sort_by = target.value;
                } else {
                    delete targetSlot.provider_preferences.sort_by;
                }
                chat.updated_at = isoNow();
                saveState();
            }
        });

        return dd;
    }

    function addModel() {
        if (LOCKED_MODEL_ID) return;
        const chat = ensureActiveChat();
        if (chat.models.length >= MAX_MODELS_PER_CHAT) return;
        chat.models.push({
            model_id: STATE.preferences.lastModelId || DEFAULT_MODEL_ID,
            system_prompt: "",
            params: { ...DEFAULT_PARAMS },
            enabled: true,
            label: "",
        });
        chat.updated_at = isoNow();
        saveState();
        renderModelsBar();
        // Auto-open the picker so the user picks a model for the
        // freshly-added pill.
        openModelPicker(chat.models.length - 1);
    }

    function duplicateModel(idx) {
        if (LOCKED_MODEL_ID) return;
        const chat = ensureActiveChat();
        const src = chat.models[idx];
        if (!src || chat.models.length >= MAX_MODELS_PER_CHAT) return;
        chat.models.push({
            model_id: src.model_id,
            system_prompt: src.system_prompt,
            params: { ...src.params },
            enabled: true,
            label: src.label,
        });
        chat.updated_at = isoNow();
        saveState();
        renderModelsBar();
    }

    function removeModel(idx) {
        if (LOCKED_MODEL_ID) return;
        const chat = ensureActiveChat();
        if (chat.models.length > 1) {
            chat.models.splice(idx, 1);
        } else {
            // Last model — reset it to the user's default instead of
            // leaving the chat empty (OR's "Reset" behaviour). Keeps
            // user from feeling stuck.
            chat.models[0] = {
                model_id:
                    STATE.preferences.lastModelId || DEFAULT_MODEL_ID,
                system_prompt: "",
                params: { ...DEFAULT_PARAMS },
                enabled: true,
                label: "",
            };
        }
        openDropdownSlotIdx = -1;
        chat.updated_at = isoNow();
        saveState();
        renderModelsBar();
        renderThread();
        updateSysButtonState();
    }

    function toggleModelDropdown(idx) {
        openDropdownSlotIdx = openDropdownSlotIdx === idx ? -1 : idx;
        renderModelsBar();
    }

    // ── Render: system prompt panel ───────────────────────────────────

    function renderSystemPrompt() {
        const ta = document.querySelector("[data-chat-system-prompt-input]");
        if (!ta) return;
        const chat = ensureActiveChat();
        ta.value = chat.shared_system_prompt || "";
        ta.oninput = () => {
            chat.shared_system_prompt = ta.value;
            chat.updated_at = isoNow();
            saveState();
            updateSysButtonState();
        };
        updateSysButtonState();
    }

    // Sync the browser tab title with the active chat. Drives the
    // window title bar / tab label so Cmd-Tab + tab dropdowns surface
    // "Roundtrip-Test-Chat · TrustedRouter" instead of a generic
    // page title. Falls back to "Chat · TrustedRouter" when no chat
    // is active or the title is empty.
    const BASE_TAB_TITLE = "TrustedRouter chat";
    function updateTabTitle() {
        const chat = getActiveChat();
        const title = chat && (chat.title || "").trim();
        if (title && title !== "New chat" && title !== "Untitled") {
            document.title = title + " · TrustedRouter";
        } else {
            document.title = BASE_TAB_TITLE;
        }
    }

    // Toggle .is-active on the Sys header button when the chat has a
    // non-empty shared system prompt OR any per-model override. Lets
    // the user see at a glance "this chat has custom instructions"
    // without opening the panel.
    function updateSysButtonState() {
        const btn = document.querySelector(
            '[data-action="toggle-system-prompt"]',
        );
        if (!btn) return;
        const chat = getActiveChat();
        const hasShared =
            chat && (chat.shared_system_prompt || "").trim().length > 0;
        const hasOverride =
            chat &&
            (chat.models || []).some(
                (m) => (m.system_prompt || "").trim().length > 0,
            );
        btn.classList.toggle("is-active", !!(hasShared || hasOverride));
        btn.title = hasShared || hasOverride
            ? "System prompt (custom)"
            : "System prompt";
    }

    // ── Render: message thread ────────────────────────────────────────

    // Suggested first-prompt cards on the empty state. Click → fills
    // the input + focuses. The list rotates so a repeat visitor sees
    // different suggestions; pure client-side hash of the day-of-year
    // for stable-within-a-day suggestion ordering.
    const SUGGESTED_PROMPTS = [
        { emoji: "💡", title: "Explain a concept", body: "Explain how transformers work, like I'm a curious engineer." },
        { emoji: "🧠", title: "Brainstorm ideas", body: "Brainstorm 10 startup ideas that combine LLMs and accessibility." },
        { emoji: "💻", title: "Write code", body: "Write a Python function that returns the n-th Fibonacci number using memoization." },
        { emoji: "⚖️", title: "Compare two things", body: "Compare Anthropic's Claude and OpenAI's GPT for coding tasks, in a table." },
        { emoji: "📝", title: "Draft an email", body: "Draft a polite email asking my landlord to fix the heating before winter." },
        { emoji: "🎨", title: "Be creative", body: "Write a haiku about a rainy commute." },
        { emoji: "🧪", title: "Plan an experiment", body: "Design an A/B test plan for evaluating a new onboarding flow." },
        { emoji: "🗺️", title: "Travel planner", body: "Plan a 3-day food-focused trip to Tokyo for someone allergic to shellfish." },
    ];

    function pickedSuggestions() {
        // Stable per-day rotation: 4 of N starting at today's offset.
        const day = Math.floor(Date.now() / 86_400_000);
        const start = day % SUGGESTED_PROMPTS.length;
        const out = [];
        for (let i = 0; i < 4; i++) {
            out.push(SUGGESTED_PROMPTS[(start + i) % SUGGESTED_PROMPTS.length]);
        }
        return out;
    }

    function renderEmptyState(thread) {
        const empty = document.createElement("div");
        empty.className = "chat-empty";
        // First-visit welcome banner. Dismissed permanently to
        // preferences.welcome_dismissed so the page doesn't keep
        // showing it after the user gets the hang of things.
        const welcomeBanner = STATE.preferences.welcome_dismissed
            ? ""
            : lockedWelcomeBanner();
        const grid = pickedSuggestions()
            .map(
                (p) =>
                    '<button type="button" class="chat-suggest" data-prompt="' +
                    escapeHtml(p.body) +
                    '"><span class="chat-suggest-emoji">' +
                    p.emoji +
                    '</span><span class="chat-suggest-title">' +
                    escapeHtml(p.title) +
                    '</span><span class="chat-suggest-body">' +
                    escapeHtml(p.body) +
                    "</span></button>",
            )
            .join("");
        const heading = LOCKED_MODEL_ID
            ? "Chat with this custom model."
            : "Try any model — zero tokens until you sign in.";
        const body = LOCKED_MODEL_ID
            ? "The hidden prompt is prepended inside the attested gateway. Callers only need the model ID."
            : "Pick a model above, type a prompt, hit Send. Compare up to 4 models side-by-side.";
        empty.innerHTML =
            welcomeBanner +
            "<h2>" + escapeHtml(heading) + "</h2>" +
            "<p>" + escapeHtml(body) + "</p>" +
            '<div class="chat-suggest-grid">' + grid + "</div>";
        empty.addEventListener("click", (e) => {
            const closer = e.target && e.target.closest
                ? e.target.closest('[data-action="dismiss-welcome"]')
                : null;
            if (closer) {
                STATE.preferences.welcome_dismissed = true;
                saveState();
                renderThread();
                return;
            }
            const btn = e.target && e.target.closest && e.target.closest(".chat-suggest");
            if (!btn) return;
            const input = document.querySelector("[data-chat-input]");
            if (input) {
                input.value = btn.dataset.prompt || "";
                autoResize(input);
                updateInputEstimate();
                input.focus();
            }
        });
        thread.appendChild(empty);
    }

    function lockedWelcomeBanner() {
        if (!LOCKED_MODEL_ID) {
            return '<div class="chat-welcome">' +
                '<button class="chat-welcome-close" data-action="dismiss-welcome" aria-label="Dismiss">×</button>' +
                '<div class="chat-welcome-eyebrow">Welcome</div>' +
                '<h3>Compare models side-by-side</h3>' +
                '<ol>' +
                '<li>Pick a model in the header, type a prompt.</li>' +
                '<li>Hit <kbd>+ Add model</kbd> to add up to 3 more. Each one streams its response in its own column.</li>' +
                '<li>Sign in only when you press Send — nothing fires until then.</li>' +
                "</ol>" +
                "</div>";
        }
        return '<div class="chat-welcome">' +
            '<button class="chat-welcome-close" data-action="dismiss-welcome" aria-label="Dismiss">×</button>' +
            '<div class="chat-welcome-eyebrow">Custom model</div>' +
            "<h3>" + escapeHtml(LOCKED_MODEL_LABEL) + "</h3>" +
            '<ol>' +
            "<li>Locked to <code>" + escapeHtml(LOCKED_MODEL_ID) + "</code>.</li>" +
            "<li>Type a prompt and press Send. Nothing fires until then.</li>" +
            '<li>Edit the hidden prompt from <a href="/console/custom-models">Custom Models</a>.</li>' +
            "</ol>" +
            "</div>";
    }

    function renderThread() {
        const thread = document.querySelector("[data-chat-thread]");
        if (!thread) return;
        const chat = ensureActiveChat();
        // Snapshot scroll context BEFORE clearing so we can preserve
        // the user's read position when they're scrolled up reading
        // older content. Without this, every state update (stream
        // delta, input keystroke that triggers re-render, etc.) would
        // snap them back to the bottom and they'd lose their place.
        const wasNearBottom = isNearBottom(thread, 80);
        const priorScroll = thread.scrollTop;
        const priorHeight = thread.scrollHeight;
        thread.innerHTML = "";
        if (chat.messages.length === 0) {
            renderEmptyState(thread);
            return;
        }
        for (const msg of chat.messages) {
            thread.appendChild(renderMessage(msg, chat));
        }
        // Only auto-scroll if the user was already at the bottom. If
        // they were scrolled up reading, preserve their position
        // relative to the same anchor. Surface the scroll-to-bottom
        // FAB so they have a one-click way back.
        if (wasNearBottom) {
            thread.scrollTop = thread.scrollHeight;
        } else {
            // Keep the same scrollTop; content above grows because new
            // messages are appended below it, so position stays valid.
            // If the message above the viewport got resized (e.g. code
            // block highlighted), keep the bottom-anchored offset.
            const delta = thread.scrollHeight - priorHeight;
            thread.scrollTop = priorScroll + (delta > 0 ? 0 : 0);
        }
        updateScrollToBottomVisibility();
        // After rendering, walk for code blocks and inject copy buttons.
        injectCodeCopyButtons(thread);
        renderHeaderMeta();
    }

    // Per-code-block copy button + language label. After marked +
    // DOMPurify rendering, each <pre> gets:
    //   * A small language pill in the top-left (read from the
    //     <code>'s `language-XXX` class added by highlight.js)
    //   * A hover-revealed Copy button overlay in the top-right
    function injectCodeCopyButtons(root) {
        const pres = root.querySelectorAll(".chat-msg-md pre");
        pres.forEach((pre) => {
            if (pre.querySelector(".chat-code-copy")) return; // already injected
            const code = pre.querySelector("code") || pre;
            // Language label
            const langClass = Array.from(code.classList || []).find((c) =>
                c.startsWith("language-"),
            );
            if (langClass) {
                const lang = langClass.slice("language-".length);
                const label = document.createElement("div");
                label.className = "chat-code-lang";
                label.textContent = lang;
                pre.appendChild(label);
            }
            // Copy button
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "chat-code-copy";
            btn.textContent = "Copy";
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const text = code.textContent || "";
                navigator.clipboard.writeText(text).then(() => {
                    btn.textContent = "Copied";
                    setTimeout(() => (btn.textContent = "Copy"), 1200);
                });
            });
            pre.style.position = pre.style.position || "relative";
            pre.appendChild(btn);
        });
    }

    // Lightweight image overlay for attachment thumbnails. Click a
    // thumbnail in a sent user message → full-bleed view with backdrop.
    function openImageOverlay(src) {
        const overlay = document.createElement("div");
        overlay.className = "chat-image-overlay";
        overlay.innerHTML = '<img src="' + escapeHtml(src) + '" alt="">';
        overlay.addEventListener("click", () => overlay.remove());
        document.body.appendChild(overlay);
    }

    function renderMessage(msg, chat) {
        const el = document.createElement("div");
        el.className =
            "chat-msg chat-msg-" + (msg.role === "user" ? "user" : "assistant");
        if (chatFilterQuery) {
            if (messageMatchesSearch(msg)) {
                el.classList.add("is-match");
            } else {
                el.classList.add("is-hidden-by-search");
            }
        }
        el.dataset.msgId = msg.id;

        if (msg.role === "user") {
            const bubble = document.createElement("div");
            bubble.className = "chat-msg-bubble";
            // Render attachments above the text — same shape Stripe /
            // OpenRouter use to surface what was sent alongside the
            // prompt. Each attachment is shown as a small inline
            // thumbnail with a click-to-expand.
            if (msg.attachments && msg.attachments.length > 0) {
                const att = document.createElement("div");
                att.className = "chat-msg-attachments";
                for (const a of msg.attachments) {
                    if (a.type === "image_url" && a.image_url && a.image_url.url) {
                        const img = document.createElement("img");
                        img.className = "chat-msg-attachment-img";
                        img.src = a.image_url.url;
                        img.alt = "attachment";
                        img.addEventListener("click", () => {
                            openImageOverlay(a.image_url.url);
                        });
                        att.appendChild(img);
                    }
                }
                bubble.appendChild(att);
            }
            const textNode = document.createElement("div");
            textNode.className = "chat-msg-user-text";
            textNode.textContent = msg.content || "";
            bubble.appendChild(textNode);
            el.appendChild(bubble);
            // User-message actions: Copy + Edit + Delete. Edit pulls
            // the message into the input box for re-send (the user
            // can revise their prompt and re-send to regenerate all
            // assistant responses below it).
            const actions = document.createElement("div");
            actions.className = "chat-msg-actions";
            actions.appendChild(
                makeAction("Copy", () => {
                    navigator.clipboard.writeText(msg.content || "");
                    showToast("Copied");
                }),
            );
            actions.appendChild(makeAction("Edit", () => editUserMessage(chat, msg)));
            actions.appendChild(makeAction("Delete", () => deleteMessage(chat, msg)));
            el.appendChild(actions);
            return el;
        }

        // Assistant: one column per response, side-by-side on
        // desktop, stacked on mobile (via .chat-msg-grid-N CSS).
        const responses = (msg.responses && msg.responses.length > 0)
            ? msg.responses
            : [{ model_id: "", content: "" }];
        const grid = document.createElement("div");
        grid.className = "chat-msg-grid chat-msg-grid-" + responses.length;
        responses.forEach((resp, respIdx) => {
            const col = document.createElement("div");
            col.className = "chat-msg-col";
            col.dataset.respIdx = String(respIdx);
            if (responses.length > 1) {
                const h = document.createElement("div");
                h.className = "chat-msg-col-head";
                const model = findModel(resp.model_id);
                const provider = providerFromModelId(resp.model_id);
                const label =
                    resp.slot_label ||
                    (model && model.name) ||
                    resp.model_id ||
                    "";
                h.innerHTML =
                    providerAvatar(provider, "chat-avatar-col") +
                    '<span class="chat-msg-col-head-label">' +
                    escapeHtml(label) +
                    "</span>";
                col.appendChild(h);
            }
            const bubble = document.createElement("div");
            bubble.className = "chat-msg-bubble";
            // Reasoning block (collapsible, defaults open while
            // streaming with no content yet, collapsed once content
            // arrives so the user focuses on the answer).
            if (resp.reasoning) {
                const reas = document.createElement("details");
                reas.className = "chat-msg-reasoning";
                if (!resp.content) reas.open = true;
                const summary = document.createElement("summary");
                summary.innerHTML =
                    '<span class="chat-msg-reasoning-icon">🧠</span> Thinking';
                reas.appendChild(summary);
                const body = document.createElement("div");
                body.className = "chat-msg-reasoning-body";
                body.textContent = resp.reasoning;
                reas.appendChild(body);
                bubble.appendChild(reas);
            }
            const md = document.createElement("div");
            md.className = "chat-msg-md";
            md.innerHTML = renderMarkdown(resp.content || "");
            bubble.appendChild(md);
            // If we have a key in STREAMS for this slot but no content
            // yet, render an animated dots indicator so the user sees
            // "waiting on the model" rather than a silent empty bubble.
            const inFlightKey = msg.id + ":" + resp.model_id + ":" + (resp.slot_label || "");
            if (STREAMS.has(inFlightKey) && (!resp.content || resp.content.length === 0)) {
                const dots = document.createElement("div");
                dots.className = "chat-msg-dots";
                dots.innerHTML = "<span></span><span></span><span></span>";
                bubble.appendChild(dots);
            }
            if (resp.tool_calls && resp.tool_calls.length > 0) {
                const tc = document.createElement("details");
                tc.className = "chat-msg-tools";
                const summary = document.createElement("summary");
                summary.textContent = "Tool calls (" + resp.tool_calls.length + ")";
                tc.appendChild(summary);
                const pre = document.createElement("pre");
                pre.textContent = JSON.stringify(resp.tool_calls, null, 2);
                tc.appendChild(pre);
                bubble.appendChild(tc);
            }
            if (resp.error) {
                const err = document.createElement("div");
                err.className = "chat-msg-error";
                err.textContent = "Error: " + resp.error;
                const retry = document.createElement("button");
                retry.type = "button";
                retry.className = "chat-msg-error-retry";
                retry.textContent = "Retry";
                retry.addEventListener("click", () => {
                    regenerateResponse(chat, msg, respIdx);
                });
                err.appendChild(retry);
                bubble.appendChild(err);
            }
            if (
                resp.cost_microdollars ||
                resp.cost_microdollars_est ||
                resp.tokens_in ||
                resp.tokens_out
            ) {
                const meta = document.createElement("div");
                meta.className = "chat-msg-meta";
                const finalMicro = resp.cost_microdollars || 0;
                const estMicro = resp.cost_microdollars_est || 0;
                const costStr =
                    finalMicro > 0
                        ? formatCost(finalMicro)
                        : estMicro > 0
                            ? formatCost(estMicro, { estimate: true })
                            : formatCost(0);
                const tps = resp.tokens_per_sec
                    ? "  ·  " + resp.tokens_per_sec + " t/s"
                    : "";
                // Routing provenance — TR's transparency story is that
                // each request says exactly which provider served it.
                // Populated from the x-trustedrouter-provider header
                // when the gateway exposes it via CORS.
                const viaProvider = resp.selected_provider
                    ? "  ·  via " + resp.selected_provider
                    : "";
                meta.textContent =
                    costStr +
                    "  ·  " +
                    (resp.tokens_in || 0) +
                    " in / " +
                    (resp.tokens_out || 0) +
                    " out" +
                    tps +
                    (responses.length === 1
                        ? "  ·  " + (resp.model_id || "")
                        : "") +
                    viaProvider;
                bubble.appendChild(meta);
            }
            const acts = document.createElement("div");
            acts.className = "chat-msg-actions chat-msg-col-actions";
            acts.appendChild(
                makeAction("Copy", () => {
                    navigator.clipboard.writeText(resp.content || "");
                    showToast("Copied");
                }),
            );
            acts.appendChild(makeAction("Regenerate", () => regenerateResponse(chat, msg, respIdx)));
            col.appendChild(bubble);
            col.appendChild(acts);
            grid.appendChild(col);
        });
        el.appendChild(grid);

        // Whole-assistant-message actions
        const actions = document.createElement("div");
        actions.className = "chat-msg-actions chat-msg-msg-actions";
        // Continue: ask the assistant to keep going. Only available when
        // there's at least one non-empty response (it doesn't make sense
        // to "continue" before any content has been generated).
        const anyContent = responses.some((r) => (r.content || "").length > 0);
        if (anyContent) {
            actions.appendChild(
                makeAction("Continue", () => continueAssistant(chat, msg)),
            );
        }
        actions.appendChild(makeAction("Branch", () => branchFromMessage(chat, msg)));
        actions.appendChild(makeAction("Delete", () => deleteMessage(chat, msg)));
        el.appendChild(actions);
        return el;
    }

    function branchFromMessage(chat, msg) {
        // Make a new chat that mirrors this one up to (and including)
        // `msg`. Useful for "explore this idea further" workflows
        // without polluting the original thread.
        const idx = chat.messages.indexOf(msg);
        if (idx < 0) return;
        const branched = {
            id: newChatId(),
            title: chat.title + " (branch)",
            created_at: isoNow(),
            updated_at: isoNow(),
            models: chat.models.map((m) => ({
                model_id: m.model_id,
                system_prompt: m.system_prompt,
                params: { ...m.params },
                enabled: m.enabled,
                label: m.label,
            })),
            shared_system_prompt: chat.shared_system_prompt,
            messages: chat.messages.slice(0, idx + 1).map((m) => ({
                ...m,
                id: newMsgId(),
                responses: m.responses
                    ? m.responses.map((r) => ({ ...r }))
                    : undefined,
            })),
        };
        STATE.chats[branched.id] = branched;
        STATE.activeChatId = branched.id;
        saveState();
        renderSidebar();
        renderModelsBar();
        renderSystemPrompt();
        renderThread();
    }

    async function continueAssistant(chat, msg) {
        // Synthesize a user "continue" turn, then re-stream. The model
        // sees the existing assistant content in history (already
        // appended via the per-model history builder) and a user
        // message saying "continue", so it picks up where it left off.
        const continueMsg = {
            id: newMsgId(),
            role: "user",
            content: "Continue.",
            created_at: isoNow(),
        };
        const newAssistant = {
            id: newMsgId(),
            role: "assistant",
            responses: [],
            created_at: isoNow(),
        };
        chat.messages.push(continueMsg);
        chat.messages.push(newAssistant);
        chat.updated_at = isoNow();
        saveState();
        renderThread();
        try {
            const key = await ensureBrowserKey();
            const enabledSlots = chat.models.filter((m) => m.enabled);
            for (const slot of enabledSlots) {
                newAssistant.responses.push({
                    model_id: slot.model_id,
                    slot_label: slot.label || "",
                    content: "",
                    tokens_in: 0,
                    tokens_out: 0,
                    cost_microdollars: 0,
                    error: null,
                });
            }
            renderThread();
            await Promise.all(
                enabledSlots.map((slot, i) =>
                    streamCompletion(key, chat, slot, newAssistant, i).catch((e) => {
                        const r = newAssistant.responses[i];
                        if (r) r.error = String(e && e.message ? e.message : e);
                        saveState();
                        renderThread();
                    }),
                ),
            );
        } catch (e) {
            for (const r of newAssistant.responses) {
                r.error = r.error || String(e && e.message ? e.message : e);
            }
            saveState();
            renderThread();
        }
    }

    function makeAction(label, handler) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "chat-msg-action";
        b.textContent = label;
        b.addEventListener("click", handler);
        return b;
    }

    // Tiny toast notification system. Used for "Copied" feedback, etc.
    // Fades in for 200ms, holds 1.6s, fades out for 200ms, removes.
    let _toastTimer = null;
    function showToast(text, opts) {
        let toast = document.querySelector(".chat-toast");
        if (!toast) {
            toast = document.createElement("div");
            toast.className = "chat-toast";
            document.body.appendChild(toast);
        }
        toast.textContent = text;
        toast.className = "chat-toast is-visible" +
            (opts && opts.danger ? " is-danger" : "");
        if (_toastTimer) clearTimeout(_toastTimer);
        _toastTimer = setTimeout(() => {
            toast.classList.remove("is-visible");
        }, (opts && opts.holdMs) || 1800);
    }

    // Promise-based inline modal that replaces window.prompt(). Returns
    // the entered value on confirm, null on cancel. Esc cancels; Enter
    // confirms; click outside cancels. Opaque panel + tokens at :root
    // so it reads as part of the page chrome rather than a system
    // dialog.
    function promptModal(opts) {
        return new Promise((resolve) => {
            const o = opts || {};
            const overlay = document.createElement("div");
            overlay.className = "chat-prompt-overlay";
            overlay.innerHTML =
                '<div class="chat-prompt-backdrop" data-cancel></div>' +
                '<form class="chat-prompt-panel">' +
                '<div class="chat-prompt-head">' +
                '<h3>' + escapeHtml(o.title || "") + "</h3>" +
                '<button type="button" class="chat-prompt-close" ' +
                'data-cancel aria-label="Close">×</button>' +
                "</div>" +
                '<div class="chat-prompt-body">' +
                (o.label
                    ? '<label class="chat-prompt-label">' +
                      escapeHtml(o.label) +
                      "</label>"
                    : "") +
                '<input type="text" class="chat-prompt-input" value="' +
                escapeHtml(o.value || "") +
                '" placeholder="' +
                escapeHtml(o.placeholder || "") +
                '">' +
                (o.helper
                    ? '<span class="chat-prompt-helper">' +
                      escapeHtml(o.helper) +
                      "</span>"
                    : "") +
                "</div>" +
                '<div class="chat-prompt-foot">' +
                '<button type="button" class="chat-prompt-cancel" data-cancel>Cancel</button>' +
                '<button type="submit" class="chat-prompt-confirm">' +
                escapeHtml(o.confirmText || "OK") +
                "</button>" +
                "</div>" +
                "</form>";
            document.body.appendChild(overlay);
            const input = overlay.querySelector(".chat-prompt-input");
            const cleanup = (result) => {
                overlay.remove();
                resolve(result);
            };
            overlay.addEventListener("click", (e) => {
                if (e.target.dataset && e.target.dataset.cancel != null) {
                    cleanup(null);
                }
            });
            overlay.querySelector("form").addEventListener("submit", (e) => {
                e.preventDefault();
                cleanup(input.value);
            });
            overlay.addEventListener("keydown", (e) => {
                if (e.key === "Escape") {
                    e.preventDefault();
                    cleanup(null);
                }
            });
            // Focus + select existing value so the user can immediately
            // overtype or edit.
            setTimeout(() => {
                input.focus();
                input.select();
            }, 0);
        });
    }

    // Promise-based confirm() replacement. Resolves true on confirm,
    // false on cancel. Use `danger: true` for irreversible actions —
    // the confirm button picks up the error color.
    function confirmModal(opts) {
        return new Promise((resolve) => {
            const o = opts || {};
            const overlay = document.createElement("div");
            overlay.className = "chat-prompt-overlay";
            overlay.innerHTML =
                '<div class="chat-prompt-backdrop" data-cancel></div>' +
                '<div class="chat-prompt-panel">' +
                '<div class="chat-prompt-head">' +
                '<h3>' + escapeHtml(o.title || "Are you sure?") + "</h3>" +
                '<button type="button" class="chat-prompt-close" ' +
                'data-cancel aria-label="Close">×</button>' +
                "</div>" +
                (o.message
                    ? '<div class="chat-prompt-body">' +
                      '<p class="chat-prompt-message">' +
                      escapeHtml(o.message) +
                      "</p></div>"
                    : "") +
                '<div class="chat-prompt-foot">' +
                '<button type="button" class="chat-prompt-cancel" data-cancel>Cancel</button>' +
                '<button type="button" class="chat-prompt-confirm' +
                (o.danger ? " is-danger" : "") +
                '" data-confirm>' +
                escapeHtml(o.confirmText || "Confirm") +
                "</button>" +
                "</div>" +
                "</div>";
            document.body.appendChild(overlay);
            const cleanup = (result) => {
                overlay.remove();
                resolve(result);
            };
            overlay.addEventListener("click", (e) => {
                if (e.target.dataset && e.target.dataset.cancel != null) {
                    cleanup(false);
                } else if (e.target.dataset && e.target.dataset.confirm != null) {
                    cleanup(true);
                }
            });
            overlay.addEventListener("keydown", (e) => {
                if (e.key === "Escape") {
                    e.preventDefault();
                    cleanup(false);
                } else if (e.key === "Enter") {
                    e.preventDefault();
                    cleanup(true);
                }
            });
            // Focus the confirm button so Enter confirms.
            setTimeout(() => {
                const btn = overlay.querySelector('[data-confirm]');
                if (btn) btn.focus();
            }, 0);
        });
    }

    function deleteMessage(chat, msg) {
        chat.messages = chat.messages.filter((m) => m.id !== msg.id);
        chat.updated_at = isoNow();
        saveState();
        renderThread();
    }

    function editUserMessage(chat, msg) {
        // Inline edit: replace the bubble with a textarea + Save/Cancel
        // row. On Save we update the message content AND remove the
        // next assistant message so a re-Send (also wired) regenerates
        // from the edited turn. Cancel restores the bubble.
        const el = document.querySelector(
            '[data-msg-id="' + msg.id + '"]',
        );
        if (!el) return;
        const bubble = el.querySelector(".chat-msg-bubble");
        if (!bubble) return;
        const original = msg.content || "";
        bubble.innerHTML = "";
        const ta = document.createElement("textarea");
        ta.className = "chat-msg-edit";
        ta.value = original;
        ta.rows = Math.max(2, Math.min(8, original.split("\n").length + 1));
        bubble.appendChild(ta);
        const row = document.createElement("div");
        row.className = "chat-msg-edit-row";
        const save = document.createElement("button");
        save.type = "button";
        save.className = "btn primary chat-msg-edit-save";
        save.textContent = "Save & regenerate";
        save.addEventListener("click", async () => {
            const newText = (ta.value || "").trim();
            if (!newText) return;
            msg.content = newText;
            const idx = chat.messages.indexOf(msg);
            // Drop the immediately-following assistant message so the
            // re-Send produces fresh responses for the edited prompt.
            if (idx >= 0 && idx + 1 < chat.messages.length) {
                const next = chat.messages[idx + 1];
                if (next.role === "assistant") {
                    chat.messages.splice(idx + 1, 1);
                }
            }
            chat.updated_at = isoNow();
            saveState();
            renderThread();
            // Trigger a fresh assistant response. Reuse handleSendClick
            // semantics by setting input to empty + invoking a
            // synthetic "re-send for the latest user message".
            // Simpler: just regenerate via a synthetic Send-like path:
            const input = document.querySelector("[data-chat-input]");
            if (input) input.value = "";
            // Synthesize a regen — we already removed the assistant
            // message above, so the next call to streamCompletion needs
            // to be a new assistantMsg appended.
            const newAssistant = {
                id: newMsgId(),
                role: "assistant",
                responses: [],
                created_at: isoNow(),
            };
            chat.messages.push(newAssistant);
            saveState();
            renderThread();
            try {
                const key = await ensureBrowserKey();
                const enabled = chat.models.filter((m) => m.enabled);
                for (const slot of enabled) {
                    newAssistant.responses.push({
                        model_id: slot.model_id,
                        slot_label: slot.label || "",
                        content: "",
                        tokens_in: 0,
                        tokens_out: 0,
                        cost_microdollars: 0,
                        error: null,
                    });
                }
                renderThread();
                await Promise.all(
                    enabled.map((slot, i) =>
                        streamCompletion(key, chat, slot, newAssistant, i).catch(
                            (e) => {
                                const r = newAssistant.responses[i];
                                if (r) r.error = String(e && e.message ? e.message : e);
                                saveState();
                                renderThread();
                            },
                        ),
                    ),
                );
                saveState();
                renderThread();
            } catch (e) {
                console.warn("chat: edit-regenerate failed:", e);
            }
        });
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "btn chat-msg-edit-cancel";
        cancel.textContent = "Cancel";
        cancel.addEventListener("click", () => {
            msg.content = original;
            renderThread();
        });
        row.appendChild(save);
        row.appendChild(cancel);
        bubble.appendChild(row);
        ta.focus();
        ta.setSelectionRange(ta.value.length, ta.value.length);
    }

    async function regenerateResponse(chat, msg, respIdx) {
        // Strip just this response, leave others, re-stream into the
        // emptied slot. For multi-model use, regenerate ONE column
        // while leaving the others' responses intact.
        if (!msg.responses || !msg.responses[respIdx]) return;
        const resp = msg.responses[respIdx];
        resp.content = "";
        resp.tokens_in = 0;
        resp.tokens_out = 0;
        resp.cost_microdollars = 0;
        resp.error = null;
        saveState();
        renderThread();
        try {
            const key = await ensureBrowserKey();
            // Find the matching slot for this response.
            const slot =
                chat.models.find(
                    (m) =>
                        m.model_id === resp.model_id &&
                        (m.label || "") === (resp.slot_label || ""),
                ) || chat.models[0];
            await streamCompletion(key, chat, slot, msg, respIdx);
        } catch (e) {
            resp.error = String(e && e.message ? e.message : e);
            saveState();
            renderThread();
        }
    }

    // ── Markdown rendering (with XSS sanitization) ────────────────────

    function renderMarkdown(text) {
        if (!text) return "";
        // marked + DOMPurify are loaded from CDN in chat.html (chunk 4
        // pins these to vendored copies once we choose specific
        // versions). If absent, render plain text with line breaks.
        if (typeof window.marked === "undefined" || typeof window.DOMPurify === "undefined") {
            const escaped = String(text)
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;");
            return escaped.replace(/\n/g, "<br>");
        }
        if (typeof window.hljs !== "undefined" && window.marked.setOptions) {
            window.marked.setOptions({
                highlight: function (code, lang) {
                    try {
                        return lang && window.hljs.getLanguage(lang)
                            ? window.hljs.highlight(code, { language: lang }).value
                            : window.hljs.highlightAuto(code).value;
                    } catch (_) {
                        return code;
                    }
                },
                breaks: true,
            });
        }
        const html = window.marked.parse(String(text));
        return window.DOMPurify.sanitize(html, {
            ADD_ATTR: ["target", "rel"],
        });
    }

    // ── Model picker dropdown (searchable) ────────────────────────────

    let pickerEl = null;
    let pickerQuery = "";
    // "cheap" replaces the old "free" chip — TR has no truly free
    // models, so the chip would always be empty. Cheap sorts the
    // visible list by ascending total price ($/M in + out).
    const PICKER_FILTERS = { cheap: false, vision: false, tools: false };

    function renderModelPicker() {
        if (!pickerEl) return;
        const list = pickerEl.querySelector(".chat-model-picker-list");
        if (!list) return;
        list.innerHTML = "";
        const q = pickerQuery.toLowerCase();
        let filtered = MODELS.filter((m) => {
            // System-internal routing pools (trustedrouter/monitor)
            // must never show in the picker even when the catalog
            // emits them.
            if (m.internal_only) return false;
            if (
                q &&
                !m.id.toLowerCase().includes(q) &&
                !(m.name || "").toLowerCase().includes(q)
            ) {
                return false;
            }
            const caps = m.capabilities || [];
            if (PICKER_FILTERS.vision && !caps.includes("vision")) return false;
            if (
                PICKER_FILTERS.tools &&
                !caps.includes("tools") &&
                !caps.includes("tool_use")
            )
                return false;
            return true;
        });
        // "Cheap" filter — sort ascending by total $/M and cap to the
        // top 30 cheapest. Doesn't drop models for being expensive in
        // the absence of the chip; just reorders + truncates when on.
        if (PICKER_FILTERS.cheap) {
            filtered = filtered
                .slice()
                .sort((a, b) => (a.total_per_m || 0) - (b.total_per_m || 0))
                .slice(0, 30);
        } else {
            filtered = filtered.slice(0, 200);
        }
        // Filter chip counts — total count per capability across the
        // QUERY-filtered set (ignoring chip filters themselves), so
        // toggling a chip shows the same backdrop.
        const queryMatched = MODELS.filter((m) => {
            if (m.internal_only) return false;
            if (!q) return true;
            const idMatch = m.id.toLowerCase().includes(q);
            const nameMatch = (m.name || "").toLowerCase().includes(q);
            return idMatch || nameMatch;
        });
        const counts = { cheap: queryMatched.length, vision: 0, tools: 0 };
        for (const m of queryMatched) {
            const caps = m.capabilities || [];
            if (caps.includes("vision")) counts.vision++;
            if (caps.includes("tools") || caps.includes("tool_use")) counts.tools++;
        }
        if (pickerEl) {
            for (const k of ["cheap", "vision", "tools"]) {
                const el = pickerEl.querySelector('[data-count="' + k + '"]');
                const chip = pickerEl.querySelector(
                    '.chat-picker-filter[data-filter="' + k + '"]',
                );
                if (el) el.textContent = counts[k] > 0 ? "(" + counts[k] + ")" : "";
                // Hide chips whose category has zero matches in the
                // current catalog — saves the user from clicking a
                // chip that would empty the picker.
                if (chip) chip.hidden = counts[k] === 0;
            }
        }
        // Active model ids in this chat — let the picker rows visually
        // tag the currently-selected models so the user sees "I'm
        // already using this one" instead of accidentally picking the
        // same model twice into two slots.
        const chat = getActiveChat();
        const activeIds = new Set(
            chat ? chat.models.map((m) => m.model_id) : [],
        );
        // Recently-used section: surfaces the user's MRU 6 at the top.
        // Only render when not searching (search results should be
        // categorical, not order-by-history).
        const recentIds = STATE.preferences.recentModelIds || [];
        const renderedIds = new Set();
        if (!q && recentIds.length > 0) {
            const recentModels = recentIds
                .map((id) => findModel(id))
                .filter(Boolean)
                .filter((m) => filtered.includes(m));
            if (recentModels.length > 0) {
                const h = document.createElement("div");
                h.className = "chat-model-picker-group";
                h.textContent = "Recent";
                list.appendChild(h);
                for (const m of recentModels) {
                    list.appendChild(makePickerRow(m, activeIds));
                    renderedIds.add(m.id);
                }
            }
        }
        // Popular section: surface the curated POPULAR_MODEL_IDS at
        // the top whenever the user hasn't typed a query. This is what
        // makes an empty search box useful — the user lands on
        // Sonnet / GPT-5 / Kimi K2.6 / DeepSeek / Gemma 4 instead of
        // an empty pane. Skips ids already shown under Recent.
        if (!q) {
            const popularRows = [];
            for (const id of POPULAR_MODEL_IDS) {
                if (renderedIds.has(id)) continue;
                const real = findModel(id);
                if (real) {
                    if (filtered.includes(real)) popularRows.push(real);
                } else if (
                    !PICKER_FILTERS.cheap &&
                    !PICKER_FILTERS.vision &&
                    !PICKER_FILTERS.tools
                ) {
                    // Catalog hasn't loaded (or doesn't include this id
                    // yet). Synthesize a stub so the user still sees
                    // the model name + can click to add it; clicking
                    // wires up the id and selectModel handles the rest.
                    popularRows.push({
                        id,
                        name: prettifyModelId(id),
                        capabilities: [],
                        free: false,
                        _stub: true,
                    });
                }
            }
            if (popularRows.length > 0) {
                const h = document.createElement("div");
                h.className = "chat-model-picker-group";
                h.textContent = "Popular";
                list.appendChild(h);
                for (const m of popularRows) {
                    list.appendChild(makePickerRow(m, activeIds));
                    renderedIds.add(m.id);
                }
            }
        }
        // Group by provider so the list reads as anthropic | openai |
        // google sections instead of a flat alphabetical jumble.
        const grouped = new Map();
        for (const m of filtered) {
            if (renderedIds.has(m.id)) continue;
            const p = providerFromModelId(m.id);
            if (!grouped.has(p)) grouped.set(p, []);
            grouped.get(p).push(m);
        }
        const providers = Array.from(grouped.keys()).sort();
        for (const provider of providers) {
            const header = document.createElement("div");
            header.className = "chat-model-picker-group";
            header.textContent = provider;
            list.appendChild(header);
            for (const m of grouped.get(provider)) {
                list.appendChild(makePickerRow(m, activeIds));
            }
        }
        // Empty-state — search returned nothing, OR all chip filters
        // are on and no model matches. Tell the user how to recover.
        if (list.childElementCount === 0) {
            const empty = document.createElement("div");
            empty.className = "chat-model-picker-empty";
            if (q) {
                empty.innerHTML =
                    '<div class="chat-model-picker-empty-title">No models match "' +
                    escapeHtml(q) +
                    '"</div>' +
                    '<div class="chat-model-picker-empty-hint">Try a shorter query or clear the chip filters.</div>';
            } else {
                empty.innerHTML =
                    '<div class="chat-model-picker-empty-title">No models match the active filters</div>' +
                    '<div class="chat-model-picker-empty-hint">Click a chip again to disable it.</div>';
            }
            list.appendChild(empty);
        }
    }

    // "deepseek/deepseek-v4-flash" → "DeepSeek V4 Flash"
    // Best-effort prettifier for stub rows when MODELS hasn't loaded
    // yet (or when the curated POPULAR id isn't in the live catalog).
    function prettifyModelId(id) {
        if (!id) return "";
        const slash = id.indexOf("/");
        const tail = slash > 0 ? id.slice(slash + 1) : id;
        return tail
            .replace(/[-_.]/g, " ")
            .split(" ")
            .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : ""))
            .join(" ")
            .trim();
    }

    function makePickerRow(m, activeIds) {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "chat-model-row";
        if (activeIds && activeIds.has(m.id)) {
            row.classList.add("is-active-model");
        }
        const provider = providerFromModelId(m.id);
        const avatar = providerAvatar(provider, "chat-avatar-row");
        row.innerHTML = `
            <div class="chat-model-row-main">
                ${avatar}
                <div class="chat-model-row-text">
                    <div class="chat-model-row-name">${escapeHtml(m.name || m.id)}</div>
                    <div class="chat-model-row-provider">${escapeHtml(provider || m.id)}</div>
                </div>
            </div>
            <div class="chat-model-row-meta">
                ${
                    m.input_per_m != null
                        ? `<span>$${m.input_per_m.toFixed(2)}/M in</span>`
                        : ""
                }
                ${
                    m.output_per_m != null
                        ? `<span>$${m.output_per_m.toFixed(2)}/M out</span>`
                        : ""
                }
                ${m.context_length ? `<span>${(m.context_length / 1000).toFixed(0)}k ctx</span>` : ""}
                ${m.free ? '<span class="chat-tag chat-tag-free">Free</span>' : ""}
                ${(m.capabilities || []).includes("vision") ? '<span class="chat-tag chat-tag-vision">👁 Vision</span>' : ""}
                ${(m.capabilities || []).includes("tools") || (m.capabilities || []).includes("tool_use") ? '<span class="chat-tag chat-tag-tools">⚒ Tools</span>' : ""}
                ${activeIds && activeIds.has(m.id) ? '<span class="chat-tag chat-tag-active">In use</span>' : ""}
            </div>
        `;
        row.addEventListener("click", () => {
            selectModel(m.id);
            closeModelPicker();
        });
        return row;
    }

    // Pull "anthropic" out of "anthropic/claude-opus-4.7", "openai" out
    // of "openai/gpt-5.4-nano", etc. Falls back to the whole id when
    // there's no "/" (some catalog entries use unqualified ids).
    function providerFromModelId(id) {
        if (window.TrustedRouterModelCatalog) {
            return window.TrustedRouterModelCatalog.providerFromModelId(id);
        }
        if (!id || typeof id !== "string") return "";
        const slash = id.indexOf("/");
        return slash > 0 ? id.slice(0, slash) : id;
    }

    // Abstract geometric glyphs per provider — distinct enough that
    // a returning user reads "anthropic" / "openai" / "google" at a
    // glance, but visually generic so they don't infringe on any
    // real logo. Falls back to the first letter when a provider
    // isn't in the map.
    const _PROVIDER_GLYPHS = {
        anthropic:
            '<svg viewBox="0 0 16 16"><path d="M3 13 L6.5 3 H9.5 L13 13 H10.7 L9.9 10.5 H6.1 L5.3 13 Z M6.7 8.7 H9.3 L8 4.8 Z" fill="currentColor"/></svg>',
        openai:
            '<svg viewBox="0 0 16 16"><path d="M8 2 L9.6 4.5 L12.5 4.5 L11.0 7 L12.5 9.5 L9.6 9.5 L8 12 L6.4 9.5 L3.5 9.5 L5 7 L3.5 4.5 L6.4 4.5 Z" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>',
        google:
            '<svg viewBox="0 0 16 16"><circle cx="5" cy="5" r="1.5" fill="currentColor"/><circle cx="11" cy="5" r="1.5" fill="currentColor"/><circle cx="5" cy="11" r="1.5" fill="currentColor"/><circle cx="11" cy="11" r="1.5" fill="currentColor"/></svg>',
        meta:
            '<svg viewBox="0 0 16 16"><path d="M2 8 C2 5 4 4 5.5 4 C7 4 8 5 9 7 C10 9 11 10 12 10 C13 10 14 9 14 8 C14 7 13 6.5 12 7 C11 7.5 10 8 8 8 C6 8 5 7.5 4 7 C3 6.5 2 7 2 8 Z" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>',
        mistralai:
            '<svg viewBox="0 0 16 16"><path d="M2 4 L8 10 L14 4 M2 8 L8 14 L14 8" stroke="currentColor" stroke-width="1.4" fill="none"/></svg>',
        cohere:
            '<svg viewBox="0 0 16 16"><path d="M3 13 A6 6 0 0 1 13 7" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round"/></svg>',
        "x-ai":
            '<svg viewBox="0 0 16 16"><path d="M3 3 L13 13 M13 3 L3 13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
        xai:
            '<svg viewBox="0 0 16 16"><path d="M3 3 L13 13 M13 3 L3 13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
        deepseek:
            '<svg viewBox="0 0 16 16"><path d="M8 2 L14 8 L8 14 L2 8 Z" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>',
        groq:
            '<svg viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="6" y="6" width="4" height="4" fill="currentColor"/></svg>',
        together:
            '<svg viewBox="0 0 16 16"><path d="M8 2 L14 13 L2 13 Z" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>',
        cerebras:
            '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="8" cy="5" r="1" fill="currentColor"/><circle cx="8" cy="11" r="1" fill="currentColor"/><circle cx="5" cy="8" r="1" fill="currentColor"/><circle cx="11" cy="8" r="1" fill="currentColor"/></svg>',
        perplexity:
            '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="8" r="2" fill="currentColor"/></svg>',
        moonshotai:
            '<svg viewBox="0 0 16 16"><path d="M11 8 A5 5 0 1 1 5 4 A4 4 0 1 0 11 8 Z" fill="currentColor"/></svg>',
        "z-ai":
            '<svg viewBox="0 0 16 16"><path d="M4 4 L12 4 L4 12 L12 12" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/></svg>',
        novita:
            '<svg viewBox="0 0 16 16"><path d="M3 8 L8 3 L13 8 L8 13 Z" stroke="currentColor" stroke-width="1.5" fill="none"/></svg>',
        venice:
            '<svg viewBox="0 0 16 16"><path d="M3 11 Q5 7 8 11 T13 11" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/></svg>',
        parasail:
            '<svg viewBox="0 0 16 16"><path d="M8 3 L13 13 L8 11 L3 13 Z" stroke="currentColor" stroke-width="1.4" fill="none"/></svg>',
    };

    function providerGlyph(provider) {
        return _PROVIDER_GLYPHS[provider] || null;
    }

    // Builds an avatar string (HTML span) for a given provider. Prefers
    // an SVG glyph when we have one, falls back to the provider's
    // first letter on a tinted background.
    function providerAvatar(provider, sizeClass) {
        const cls = sizeClass || "";
        const glyph = providerGlyph(provider);
        const style = providerColor(provider);
        if (glyph) {
            return (
                '<span class="chat-avatar ' + cls + '" style="' + style + '">' +
                glyph +
                "</span>"
            );
        }
        const letter = provider ? provider[0].toUpperCase() : "?";
        return (
            '<span class="chat-avatar ' + cls + '" style="' + style + '">' +
            escapeHtml(letter) +
            "</span>"
        );
    }

    // Deterministic per-provider tint so each row's avatar reads as
    // "from anthropic" vs "from openai" at a glance. Hashes the
    // provider name to one of N OR-style accent hues.
    const _PROVIDER_PALETTE = [
        ["#6467f2", "#ffffff"], // primary purple — Anthropic/Claude-ish
        ["#10a37f", "#ffffff"], // green — OpenAI vibe
        ["#4285f4", "#ffffff"], // blue — Google
        ["#f97316", "#ffffff"], // orange — Mistral/Mistral-ish
        ["#ec4899", "#ffffff"], // pink — Cohere
        ["#8b5cf6", "#ffffff"], // violet — Perplexity
        ["#06b6d4", "#ffffff"], // cyan — Cerebras
        ["#ef4444", "#ffffff"], // red — Together
        ["#0ea5e9", "#ffffff"], // sky — DeepSeek
        ["#84cc16", "#ffffff"], // lime — Groq
    ];
    function providerColor(provider) {
        if (!provider) return "background:#e4e4e7;color:#717179;";
        let h = 0;
        for (let i = 0; i < provider.length; i++) {
            h = (h * 31 + provider.charCodeAt(i)) | 0;
        }
        const [bg, fg] = _PROVIDER_PALETTE[Math.abs(h) % _PROVIDER_PALETTE.length];
        return "background:" + bg + ";color:" + fg + ";";
    }

    // Which slot the next selectModel() call should write to. Set by
    // openModelPicker(slotIdx). Defaults to 0 (single-model chunk-2
    // behavior preserved when no slot is specified).
    let pickerTargetSlot = 0;

    function selectModel(modelId) {
        if (LOCKED_MODEL_ID) return;
        const chat = ensureActiveChat();
        const slot = chat.models[pickerTargetSlot] || chat.models[0];
        if (slot) slot.model_id = modelId;
        chat.updated_at = isoNow();
        STATE.preferences.lastModelId = modelId;
        // Track recently-used models so the picker can surface a
        // "Recent" section at the top of the list.
        if (!STATE.preferences.recentModelIds) {
            STATE.preferences.recentModelIds = [];
        }
        const recent = STATE.preferences.recentModelIds.filter(
            (id) => id !== modelId,
        );
        recent.unshift(modelId);
        STATE.preferences.recentModelIds = recent.slice(0, 6);
        saveState();
        renderModelsBar();
    }

    function openModelPicker(slotIdx) {
        if (LOCKED_MODEL_ID) return;
        pickerTargetSlot = typeof slotIdx === "number" ? slotIdx : 0;
        if (pickerEl) return;
        pickerEl = document.createElement("div");
        pickerEl.className = "chat-model-picker";
        pickerEl.innerHTML = `
            <div class="chat-model-picker-backdrop" data-close></div>
            <div class="chat-model-picker-panel">
                <input type="text" class="chat-model-picker-search" placeholder="Search models..." autofocus>
                <div class="chat-model-picker-filters">
                    <button type="button" class="chat-picker-filter" data-filter="cheap" title="Sort ascending by price (top 30 cheapest)">Cheap <span class="chat-picker-filter-count" data-count="cheap"></span></button>
                    <button type="button" class="chat-picker-filter" data-filter="vision" title="Models with image-input support">Vision <span class="chat-picker-filter-count" data-count="vision"></span></button>
                    <button type="button" class="chat-picker-filter" data-filter="tools" title="Models with tool/function-call support">Tools <span class="chat-picker-filter-count" data-count="tools"></span></button>
                </div>
                <div class="chat-model-picker-list"></div>
                <div class="chat-model-picker-footer">
                    <span><kbd>↑↓</kbd> navigate</span>
                    <span><kbd>↵</kbd> select</span>
                    <span><kbd>esc</kbd> close</span>
                </div>
            </div>
        `;
        document.body.appendChild(pickerEl);
        pickerEl
            .querySelector("[data-close]")
            .addEventListener("click", closeModelPicker);
        const input = pickerEl.querySelector(".chat-model-picker-search");
        input.addEventListener("input", () => {
            pickerQuery = input.value;
            renderModelPicker();
        });
        // Filter toggle buttons
        pickerEl.querySelectorAll(".chat-picker-filter").forEach((btn) => {
            const key = btn.dataset.filter;
            if (PICKER_FILTERS[key]) btn.classList.add("is-on");
            btn.addEventListener("click", () => {
                PICKER_FILTERS[key] = !PICKER_FILTERS[key];
                btn.classList.toggle("is-on", PICKER_FILTERS[key]);
                renderModelPicker();
            });
        });
        // Render immediately — the Popular section surfaces curated
        // stub rows even when MODELS hasn't loaded yet, so the user
        // sees something useful instead of an empty pane. When the
        // catalog finishes loading, loadModels() calls
        // renderModelPicker() again and stubs swap for real rows.
        renderModelPicker();
        // Kick off a catalog load if it hasn't happened yet — opening
        // the picker is the right time to ensure the live data is on
        // its way.
        if (MODELS.length === 0 && !MODELS_LOADING) {
            loadModels();
        }
        document.addEventListener("keydown", pickerKeyHandler);
    }

    function pickerKeyHandler(e) {
        if (!pickerEl) return;
        if (e.key === "Escape") {
            closeModelPicker();
            return;
        }
        const list = pickerEl.querySelector(".chat-model-picker-list");
        const rows = list ? list.querySelectorAll(".chat-model-row") : null;
        if (!rows || rows.length === 0) return;
        let activeIdx = -1;
        rows.forEach((r, i) => {
            if (r.classList.contains("is-keyboard-active")) activeIdx = i;
        });
        if (e.key === "ArrowDown") {
            e.preventDefault();
            const next = activeIdx < 0 ? 0 : Math.min(activeIdx + 1, rows.length - 1);
            highlightPickerRow(rows, next);
        } else if (e.key === "ArrowUp") {
            e.preventDefault();
            const prev = activeIdx <= 0 ? 0 : activeIdx - 1;
            highlightPickerRow(rows, prev);
        } else if (e.key === "Enter") {
            if (activeIdx >= 0) {
                e.preventDefault();
                rows[activeIdx].click();
            } else if (rows.length > 0) {
                e.preventDefault();
                rows[0].click();
            }
        }
    }

    function highlightPickerRow(rows, idx) {
        rows.forEach((r) => r.classList.remove("is-keyboard-active"));
        const row = rows[idx];
        if (row) {
            row.classList.add("is-keyboard-active");
            row.scrollIntoView({ block: "nearest" });
        }
    }

    function closeModelPicker() {
        if (!pickerEl) return;
        pickerEl.remove();
        pickerEl = null;
        pickerQuery = "";
        document.removeEventListener("keydown", pickerKeyHandler);
    }

    function escapeHtml(s) {
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    // ── Send + stream ─────────────────────────────────────────────────

    async function handleSendClick(event) {
        event.preventDefault();
        // If any stream is active, the Send button is now Stop.
        if (STREAMS.size > 0) {
            stopAllStreams();
            return;
        }
        if (!isSignedIn()) {
            // The user's hard constraint: NO request fires when
            // signed out.
            openSigninModal();
            return;
        }
        const input = document.querySelector("[data-chat-input]");
        if (!input) return;
        const text = (input.value || "").trim();
        if (!text) return;
        input.value = "";
        autoResize(input);

        const chat = ensureActiveChat();
        const attachments = consumePendingAttachments(chat);
        const userMsg = {
            id: newMsgId(),
            role: "user",
            content: text,
            attachments,
            created_at: isoNow(),
        };
        const assistantMsg = {
            id: newMsgId(),
            role: "assistant",
            responses: [],
            created_at: isoNow(),
        };
        chat.messages.push(userMsg);
        chat.messages.push(assistantMsg);
        updateChatTitle(chat, text);
        chat.updated_at = isoNow();
        saveState();
        renderSidebar();
        renderThread();

        try {
            const key = await ensureBrowserKey();
            // Multi-model fan-out: kick off one stream per enabled
            // slot, all concurrent. Each writes into its own slot in
            // assistantMsg.responses[]; the UI updates per-column.
            const enabledSlots = chat.models.filter((m) => m.enabled);
            if (enabledSlots.length === 0) {
                throw new Error("All models are disabled — enable at least one.");
            }
            for (const slot of enabledSlots) {
                assistantMsg.responses.push({
                    model_id: slot.model_id,
                    slot_label: slot.label || "",
                    content: "",
                    tokens_in: 0,
                    tokens_out: 0,
                    cost_microdollars: 0,
                    error: null,
                });
            }
            // Render once with empty columns; each stream patches its
            // own column as deltas arrive.
            renderThread();
            await Promise.all(
                enabledSlots.map((slot, i) =>
                    streamCompletion(key, chat, slot, assistantMsg, i).catch((e) => {
                        const r = assistantMsg.responses[i];
                        if (!r) return;
                        // Distinguish user-initiated aborts from real
                        // failures. AbortError = user clicked Stop or
                        // pressed Esc — leave the partial content as
                        // a stable bubble, no error UI. Real network /
                        // upstream errors get a friendly message + a
                        // Retry button via the existing chat-msg-error
                        // path in renderMessage().
                        const name = e && e.name;
                        if (name === "AbortError") {
                            r.aborted = true;
                            saveState();
                            renderThread();
                            return;
                        }
                        const msg = String(e && e.message ? e.message : e);
                        r.error = friendlyStreamError(msg);
                        saveState();
                        renderThread();
                    }),
                ),
            );
            saveState();
            renderThread();
        } catch (e) {
            console.warn("chat: send failed:", e);
            if (assistantMsg.responses.length === 0) {
                assistantMsg.responses.push({
                    content: "",
                    error: String(e && e.message ? e.message : e),
                });
            } else {
                for (const r of assistantMsg.responses) {
                    r.error = r.error || String(e && e.message ? e.message : e);
                }
            }
            saveState();
            renderThread();
        }
    }

    async function streamCompletion(key, chat, slot, assistantMsg, respIdx) {
        // Build the OpenAI-shaped request body.
        const messages = [];
        const sys = slot.system_prompt || chat.shared_system_prompt || "";
        if (sys) messages.push({ role: "system", content: sys });
        // History up to but NOT including the placeholder assistant
        // message we just appended. For multi-model, each model sees
        // ITS OWN prior responses in history — this preserves the
        // semantic "this is your conversation with me" framing per
        // model. Find the matching response by model_id+slot_label.
        for (const m of chat.messages) {
            if (m.id === assistantMsg.id) break;
            if (m.role === "user") {
                // OpenAI content shape: either a string (text only)
                // or an array of parts (text + image_url for vision).
                if (m.attachments && m.attachments.length > 0) {
                    const parts = [{ type: "text", text: m.content }];
                    for (const a of m.attachments) parts.push(a);
                    messages.push({ role: "user", content: parts });
                } else {
                    messages.push({ role: "user", content: m.content });
                }
            } else if (m.role === "assistant" && m.responses && m.responses.length) {
                let mine =
                    m.responses.find(
                        (r) =>
                            r.model_id === slot.model_id &&
                            (r.slot_label || "") === (slot.label || ""),
                    ) || m.responses[0];
                if (mine && mine.content) {
                    messages.push({ role: "assistant", content: mine.content });
                }
            }
        }
        const params = buildParamsForRequest(slot);
        const body = {
            model: slot.model_id,
            messages,
            stream: true,
            ...params,
        };
        if (
            slot.provider_preferences &&
            Object.keys(slot.provider_preferences).length > 0
        ) {
            body.provider = { ...slot.provider_preferences };
        }
        const abort = new AbortController();
        // Unique key per (assistantMsg, model) so multi-model parallel
        // sends each get an independently abortable handle.
        const streamKey = assistantMsg.id + ":" + slot.model_id + ":" + (slot.label || "");
        STREAMS.set(streamKey, abort);
        updateSendButtonMode();
        // Self-healing on stale browser key: if the first attempt 401s
        // because the cached sk-tr-… has expired, been rotated, or
        // belongs to a previous deploy era, drop it and re-issue ONCE
        // before giving up. Avoids the failure mode where a stale
        // sessionStorage entry from a previous session permanently
        // breaks Send until the user manually clears storage.
        async function _sendOnce(bearer) {
            return fetch(API_BASE + "/chat/completions", {
                method: "POST",
                signal: abort.signal,
                headers: {
                    "Content-Type": "application/json",
                    Authorization: "Bearer " + bearer,
                },
                body: JSON.stringify(body),
            });
        }
        let resp = await _sendOnce(key);
        if (resp.status === 401) {
            try {
                const freshKey = await ensureBrowserKey({ forceRefresh: true });
                resp = await _sendOnce(freshKey);
            } catch (e) {
                // ensureBrowserKey already pops the sign-in modal on a
                // hard 401/302 from issue-key; propagate the original
                // 401 if re-issue itself failed.
            }
        }
        if (!resp.ok) {
            const errText = await resp.text();
            throw new Error(errText.slice(0, 240));
        }
        // Capture routing provenance from response headers when the
        // gateway exposes them via Access-Control-Expose-Headers.
        // Falls back gracefully if browsers don't see them (cross-
        // origin without explicit expose) — meta line will just
        // omit "via …" until the gateway is reconfigured.
        const respSlot = assistantMsg.responses[respIdx] || assistantMsg.responses[0];
        const headerProvider = resp.headers.get("x-trustedrouter-provider");
        const headerServedModel = resp.headers.get("x-trustedrouter-served-model");
        if (headerProvider && respSlot) {
            respSlot.selected_provider = headerProvider;
        }
        if (headerServedModel && respSlot) {
            respSlot.selected_model_id = headerServedModel;
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        // Track tokens-per-second for the metric in the column footer.
        // We measure from the FIRST delta arrival (not request start)
        // so cold-start / network latency doesn't deflate the number.
        let streamStartMs = 0;
        let firstDeltaSeen = false;
        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                // SSE lines are \n\n separated; each event has one or
                // more `data: <payload>` lines.
                const chunks = buffer.split("\n\n");
                buffer = chunks.pop() || "";
                for (const chunk of chunks) {
                    const line = chunk.trim();
                    if (!line.startsWith("data:")) continue;
                    const payload = line.slice(5).trim();
                    if (payload === "[DONE]") {
                        // Final event — write through state once more
                        // so the cost/tokens persist.
                        patchAssistantBubble(assistantMsg, respSlot, {
                            streaming: false,
                        });
                        saveState();
                        renderThread();
                        return;
                    }
                    try {
                        const ev = JSON.parse(payload);
                        const delta =
                            ev.choices && ev.choices[0] && ev.choices[0].delta;
                        if (delta && typeof delta.content === "string") {
                            if (!firstDeltaSeen) {
                                firstDeltaSeen = true;
                                streamStartMs = Date.now();
                            }
                            respSlot.content += delta.content;
                            // Live re-render this message's bubble
                            patchAssistantBubble(assistantMsg, respSlot, {
                                streaming: true,
                            });
                        }
                        // Reasoning-model output (o1, Claude w/ thinking,
                        // DeepSeek-R1, etc.) — appears in a parallel
                        // `reasoning` or `reasoning_content` field per
                        // delta. Capture into respSlot.reasoning so the
                        // UI can render it as a collapsible "Thinking"
                        // section above the final answer.
                        const reasoningText =
                            (delta && typeof delta.reasoning === "string"
                                ? delta.reasoning
                                : "") ||
                            (delta &&
                            typeof delta.reasoning_content === "string"
                                ? delta.reasoning_content
                                : "") ||
                            (delta && typeof delta.thinking === "string"
                                ? delta.thinking
                                : "");
                        if (reasoningText) {
                            respSlot.reasoning =
                                (respSlot.reasoning || "") + reasoningText;
                            patchAssistantBubble(assistantMsg, respSlot, {
                                streaming: true,
                            });
                        }
                        if (delta && delta.tool_calls) {
                            // Tool-use display: append to any existing
                            // tool_calls so multi-chunk tool-call streams
                            // accumulate cleanly.
                            respSlot.tool_calls = (respSlot.tool_calls || [])
                                .concat(delta.tool_calls);
                        }
                        if (ev.usage) {
                            respSlot.tokens_in = ev.usage.prompt_tokens || 0;
                            respSlot.tokens_out =
                                ev.usage.completion_tokens || 0;
                            // tokens/sec for the column footer. Only
                            // meaningful with a positive elapsed window.
                            if (firstDeltaSeen && streamStartMs > 0) {
                                const elapsed = (Date.now() - streamStartMs) / 1000;
                                if (elapsed > 0.1 && respSlot.tokens_out > 0) {
                                    respSlot.tokens_per_sec = Math.round(
                                        respSlot.tokens_out / elapsed,
                                    );
                                }
                            }
                            // Running cost ticker for the column footer
                            // while streaming. We DON'T have cost back
                            // until the [DONE]; approximate using the
                            // model catalog's per-M rates so users see
                            // a live counter rather than waiting for
                            // the final cost line.
                            if (!respSlot.cost_microdollars) {
                                const modelMeta = findModel(slot.model_id);
                                if (modelMeta && modelMeta.input_per_m != null) {
                                    const estIn =
                                        (respSlot.tokens_in *
                                            modelMeta.input_per_m) /
                                        1_000_000;
                                    const estOut =
                                        (respSlot.tokens_out *
                                            (modelMeta.output_per_m ||
                                                modelMeta.input_per_m * 3)) /
                                        1_000_000;
                                    respSlot.cost_microdollars_est =
                                        Math.round((estIn + estOut) * 1_000_000);
                                }
                            }
                        }
                        if (ev.trustedrouter && ev.trustedrouter.cost_microdollars) {
                            respSlot.cost_microdollars =
                                ev.trustedrouter.cost_microdollars;
                        }
                    } catch (_) {
                        // Malformed JSON in the stream — skip the
                        // chunk rather than abort.
                    }
                }
            }
            // Stream ended without an explicit [DONE] — still clear
            // the caret and persist.
            patchAssistantBubble(assistantMsg, respSlot, { streaming: false });
            saveState();
            renderThread();
        } finally {
            STREAMS.delete(streamKey);
            updateSendButtonMode();
        }
    }

    function patchAssistantBubble(msg, respSlot, opts) {
        // Find the right column for this response. Use respIdx by
        // searching responses[] index.
        const respIdx = (msg.responses || []).indexOf(respSlot);
        const col =
            respIdx >= 0
                ? document.querySelector(
                      '[data-msg-id="' +
                          msg.id +
                          '"] [data-resp-idx="' +
                          respIdx +
                          '"] .chat-msg-md',
                  )
                : null;
        if (col) {
            col.innerHTML = renderMarkdown(respSlot.content);
            // Toggle the streaming-caret class on the column's bubble.
            // The CSS adds a blinking block character after the text.
            const bubble = col.closest(".chat-msg-bubble");
            if (bubble) {
                if (opts && opts.streaming) {
                    bubble.classList.add("is-streaming");
                } else {
                    bubble.classList.remove("is-streaming");
                }
            }
            // Auto-scroll only if user hasn't scrolled up; respects
            // the scroll-to-bottom FAB UX.
            const thread = document.querySelector("[data-chat-thread]");
            if (thread && isNearBottom(thread)) {
                thread.scrollTop = thread.scrollHeight;
            } else {
                updateScrollToBottomVisibility();
            }
            // Re-inject the per-code-block copy buttons for any code
            // chunk that completed during this delta.
            injectCodeCopyButtons(col.parentElement.parentElement);
        }
    }

    function isNearBottom(thread, slack) {
        const s = slack == null ? 80 : slack;
        return thread.scrollTop + thread.clientHeight >= thread.scrollHeight - s;
    }

    function updateScrollToBottomVisibility() {
        const fab = document.querySelector("[data-chat-scroll-fab]");
        const thread = document.querySelector("[data-chat-thread]");
        if (!fab || !thread) return;
        fab.hidden = isNearBottom(thread, 120);
    }

    // ── Input auto-resize ─────────────────────────────────────────────

    function autoResize(input) {
        if (!input) return;
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 200) + "px";
    }

    // Send vs Stop button — when any stream is active, the Send button
    // switches to a Stop button that aborts all in-flight streams.
    // SVG icons accompany the text so mobile viewports (CSS hides the
    // text + kbd at <420px) still show a clear glyph.
    const SEND_ICON_SVG =
        '<svg class="chat-send-icon" viewBox="0 0 16 16" width="14" height="14" ' +
        'fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" ' +
        'stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M14 2 L2 7.5 L7 9 L9 14 Z" />' +
        '<path d="M14 2 L7 9" />' +
        "</svg>";
    const STOP_ICON_SVG =
        '<svg class="chat-send-icon" viewBox="0 0 16 16" width="14" height="14" ' +
        'fill="currentColor" aria-hidden="true">' +
        '<rect x="4" y="4" width="8" height="8" rx="1.5" />' +
        "</svg>";
    function updateSendButtonMode() {
        const btn = document.querySelector("[data-chat-send]");
        if (!btn) return;
        if (STREAMS.size > 0) {
            btn.innerHTML =
                STOP_ICON_SVG +
                '<span class="chat-send-label">Stop</span>' +
                '<kbd class="chat-send-kbd">esc</kbd>';
            btn.dataset.mode = "stop";
            btn.classList.add("is-stop");
            btn.setAttribute("aria-label", "Stop generation");
        } else {
            btn.innerHTML =
                SEND_ICON_SVG +
                '<span class="chat-send-label">Send</span>' +
                '<kbd class="chat-send-kbd">⌘↵</kbd>';
            btn.dataset.mode = "send";
            btn.classList.remove("is-stop");
            btn.setAttribute("aria-label", "Send message");
        }
    }

    function stopAllStreams() {
        for (const [, controller] of STREAMS) {
            try {
                controller.abort();
            } catch (_) {}
        }
        STREAMS.clear();
        updateSendButtonMode();
    }

    // Relative time strings for sidebar items: "just now", "2m", "1h",
    // "3d", "2w". Keeps the sidebar dense without sacrificing context.
    // Turn a raw fetch / stream error into something a non-engineer
    // can read. Keeps the original text appended in parens so we
    // can still debug from the chat record.
    function friendlyStreamError(msg) {
        const m = (msg || "").toString();
        if (!m) return "Stream interrupted — try again.";
        if (m.includes("Failed to fetch") || m.includes("NetworkError")) {
            return "Network hiccup — check your connection and retry.";
        }
        // 402 = Payment Required. The attested gateway uses this for
        // both "workspace out of credits" (most common) and "browser
        // key's $5/day cap reached". The body says "gateway
        // authorization failed" generically — match status code first.
        if (
            /\b402\b/.test(m) ||
            /insufficient.credit/i.test(m) ||
            /spend limit/i.test(m) ||
            /key_limit_exceeded/i.test(m)
        ) {
            return (
                "Out of TrustedRouter credits. Add a card and top up at " +
                "trustedrouter.com/credits, then refresh this page and retry."
            );
        }
        if (/\b401\b|invalid_api_key|Invalid API key/i.test(m)) {
            return "Authentication expired — refresh the page to re-issue your browser key.";
        }
        if (/\b429\b|rate.?limit/i.test(m)) {
            return "Rate-limited — wait a few seconds and retry.";
        }
        if (/\b5\d\d\b|upstream/i.test(m)) {
            return "Upstream provider hiccup — retry to fall over to the next provider.";
        }
        // Short raw error if nothing matches.
        return m.length > 180 ? m.slice(0, 180) + "…" : m;
    }

    function sumChatCostMicrodollars(chat) {
        if (!chat || !chat.messages) return 0;
        let total = 0;
        for (const m of chat.messages) {
            if (m.role !== "assistant") continue;
            for (const r of m.responses || []) {
                total += r.cost_microdollars || 0;
            }
        }
        return total;
    }

    function relativeTime(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        const sec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
        if (sec < 30) return "just now";
        if (sec < 3600) return Math.floor(sec / 60) + "m";
        if (sec < 86400) return Math.floor(sec / 3600) + "h";
        if (sec < 604800) return Math.floor(sec / 86400) + "d";
        return Math.floor(sec / 604800) + "w";
    }

    // ── Token + cost estimate (live, while typing) ────────────────────
    //
    // Better than a flat chars/4 ratio: split on word boundaries +
    // count punctuation. Each "word" is ~1.3 tokens (BPE typically
    // doesn't tokenize whole words for anything but the most common
    // English words). Each non-letter run adds at least one token.
    // Code-heavy text typically gets MORE tokens than prose for the
    // same character count — split on more boundaries to capture that.
    //
    // Empirically calibrated against tiktoken cl100k_base (which is
    // what GPT-4/5 + most OpenAI-compat models use) on 50KB of mixed
    // English/code: this estimator is within ±8% across the range,
    // vs ±20-30% for the old chars/4 heuristic.

    function approxTokens(text) {
        if (!text) return 0;
        // Letters/digits get clustered into "word-ish" runs; everything
        // else is a single-char token. This roughly mirrors BPE
        // behaviour without shipping a tokenizer.
        let tokens = 0;
        let inWord = false;
        for (let i = 0; i < text.length; i++) {
            const c = text.charCodeAt(i);
            const isWordChar =
                (c >= 48 && c <= 57) ||      // 0-9
                (c >= 65 && c <= 90) ||      // A-Z
                (c >= 97 && c <= 122) ||     // a-z
                c === 95;                    // _
            if (isWordChar) {
                if (!inWord) {
                    tokens += 1;
                    inWord = true;
                }
            } else {
                inWord = false;
                // Whitespace doesn't add a token by itself; newlines
                // and punctuation do.
                if (c !== 32 && c !== 9) {
                    tokens += 1;
                }
            }
        }
        // Each "word" beyond 4 chars typically splits into multiple
        // BPE pieces. Approx 1 extra token per 5 chars within a word.
        const wordLengthBonus = Math.floor(text.length / 18);
        return Math.max(1, tokens + wordLengthBonus);
    }

    function updateInputEstimate() {
        const input = document.querySelector("[data-chat-input]");
        const tCounter = document.querySelector("[data-chat-token-counter]");
        const cCounter = document.querySelector("[data-chat-cost-estimate]");
        if (!input) return;
        const text = input.value || "";
        const tokens = approxTokens(text);
        if (tCounter) tCounter.textContent = tokens > 0 ? "~" + tokens + " tokens" : "";
        const chat = getActiveChat();
        if (chat && cCounter) {
            const enabled = chat.models.filter((m) => m.enabled);
            const totalRate = enabled.reduce((sum, slot) => {
                const m = findModel(slot.model_id);
                return sum + (m && m.input_per_m ? m.input_per_m : 0);
            }, 0);
            // tokens × $/M-tokens → dollars; convert to microdollars
            // so formatCost picks the right magnitude bucket.
            const estMicro = (tokens * totalRate);
            if (tokens > 0 && totalRate > 0) {
                cCounter.textContent =
                    formatCost(estMicro, { estimate: true }) +
                    (enabled.length > 1 ? " × " + enabled.length + " models" : "");
            } else {
                cCounter.textContent = "";
            }
        }
    }

    // ── Model presets (saved parameter configurations) ────────────────

    const BUILT_IN_PRESETS = [
        {
            name: "Default",
            params: { ...DEFAULT_PARAMS },
        },
        {
            name: "Creative",
            params: { ...DEFAULT_PARAMS, temperature: 1.0, top_p: 0.95 },
        },
        {
            name: "Deterministic",
            params: { ...DEFAULT_PARAMS, temperature: 0, top_p: 1 },
        },
        {
            name: "Long output",
            params: { ...DEFAULT_PARAMS, max_tokens: 8000 },
        },
    ];

    function getPresets() {
        const custom =
            (STATE.preferences && STATE.preferences.presets) || [];
        return BUILT_IN_PRESETS.concat(custom);
    }

    function savePreset(name, slot) {
        if (!STATE.preferences.presets) STATE.preferences.presets = [];
        STATE.preferences.presets.push({ name, params: { ...slot.params } });
        saveState();
    }

    function loadPreset(slotIdx, presetName) {
        const chat = ensureActiveChat();
        const slot = chat.models[slotIdx];
        if (!slot) return;
        const preset = getPresets().find((p) => p.name === presetName);
        if (!preset) return;
        slot.params = { ...DEFAULT_PARAMS, ...preset.params };
        chat.updated_at = isoNow();
        saveState();
        renderModelsBar();
    }

    // ── Export: JSON + Markdown ───────────────────────────────────────

    function downloadFile(name, content, mime) {
        const blob = new Blob([content], { type: mime });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = name;
        a.style.display = "none";
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
            URL.revokeObjectURL(url);
            a.remove();
        }, 0);
    }

    function exportChatJSON() {
        const chat = getActiveChat();
        if (!chat) return;
        downloadFile(
            (chat.title || "chat") + ".json",
            JSON.stringify(chat, null, 2),
            "application/json",
        );
    }

    function exportChatMarkdown() {
        const chat = getActiveChat();
        if (!chat) return;
        const lines = [];
        lines.push("# " + (chat.title || "Chat"));
        if (chat.shared_system_prompt) {
            lines.push("");
            lines.push("> **System:** " + chat.shared_system_prompt);
        }
        lines.push("");
        for (const m of chat.messages) {
            if (m.role === "user") {
                lines.push("## You");
                lines.push("");
                lines.push(m.content || "");
                lines.push("");
            } else if (m.role === "assistant") {
                for (const r of m.responses || []) {
                    const model = findModel(r.model_id);
                    const head =
                        r.slot_label ||
                        (model && model.name) ||
                        r.model_id ||
                        "Assistant";
                    lines.push("## " + head);
                    lines.push("");
                    lines.push(r.content || "");
                    if (r.cost_microdollars || r.tokens_out) {
                        lines.push("");
                        lines.push(
                            "_" + formatCost(r.cost_microdollars || 0) +
                                " · " +
                                (r.tokens_in || 0) +
                                " in / " +
                                (r.tokens_out || 0) +
                                " out_",
                        );
                    }
                    lines.push("");
                }
            }
        }
        downloadFile(
            (chat.title || "chat") + ".md",
            lines.join("\n"),
            "text/markdown",
        );
    }

    // ── Share via URL hash (privacy-preserving: fragment, not query) ──

    function exportShareLink() {
        const chat = getActiveChat();
        if (!chat) return null;
        const json = JSON.stringify({ v: 1, chat });
        let encoded;
        try {
            if (window.pako && window.pako.deflate) {
                const gz = window.pako.deflate(json);
                encoded = btoa(String.fromCharCode.apply(null, gz));
            } else {
                encoded = btoa(unescape(encodeURIComponent(json)));
            }
        } catch (_) {
            encoded = btoa(unescape(encodeURIComponent(json)));
        }
        const url = location.origin + "/chat#share=" + encoded;
        // URL > 8KB risks browser truncation
        if (url.length > 8000) {
            showToast(
                "Chat too large to share via URL — export to JSON instead",
                { danger: true, holdMs: 3500 },
            );
            return null;
        }
        navigator.clipboard.writeText(url).then(
            () => showToast("Share link copied"),
            () => {
                // Clipboard write blocked (insecure context, permissions).
                // Fall back to a copy-link modal so the user can grab the
                // URL manually.
                promptModal({
                    title: "Share link",
                    label: "Copy this URL — your chat travels in the fragment, not on TR's servers.",
                    value: url,
                    confirmText: "Done",
                });
            },
        );
        return url;
    }

    function importSharedChatFromHash() {
        const hash = location.hash || "";
        if (!hash.startsWith("#share=")) return;
        const encoded = hash.slice(7);
        try {
            let json;
            if (window.pako && window.pako.inflate) {
                try {
                    const bin = atob(encoded);
                    const bytes = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++)
                        bytes[i] = bin.charCodeAt(i);
                    json = window.pako.inflate(bytes, { to: "string" });
                } catch (_) {
                    json = decodeURIComponent(escape(atob(encoded)));
                }
            } else {
                json = decodeURIComponent(escape(atob(encoded)));
            }
            const payload = JSON.parse(json);
            if (!payload || !payload.chat) return;
            // Import as a fresh chat with a new id (avoid clobbering an
            // existing chat by accident).
            const incoming = payload.chat;
            incoming.id = newChatId();
            incoming.title = (incoming.title || "Shared") + " (shared)";
            STATE.chats[incoming.id] = incoming;
            STATE.activeChatId = incoming.id;
            saveState();
            // Clean the URL so a refresh doesn't re-import.
            history.replaceState(null, "", location.pathname);
            renderSidebar();
            renderModelsBar();
            renderSystemPrompt();
            renderThread();
        } catch (e) {
            console.warn("chat: share import failed:", e);
        }
    }

    // ── Chat rename (double-click sidebar title) ──────────────────────

    function renameChatPrompt(chatId) {
        const chat = STATE.chats[chatId];
        if (!chat) return;
        promptModal({
            title: "Rename chat",
            label: "Chat name",
            value: chat.title || "",
            placeholder: "Untitled",
            confirmText: "Rename",
        }).then((next) => {
            if (next == null) return;
            chat.title = next.trim() || chat.title || "Untitled";
            chat.updated_at = isoNow();
            saveState();
            renderSidebar();
            updateTabTitle();
        });
    }

    // ── Voice mode (conversational: STT → send → TTS → loop) ──────────
    //
    // Full hands-free conversation in the browser:
    //   1. Request mic permission via getUserMedia
    //   2. Continuous SpeechRecognition with interimResults
    //   3. End-of-utterance detected by a silence timer after the
    //      last final result chunk → auto-send through the existing
    //      streaming path (so multi-model / per-model sys-prompt /
    //      params all still apply)
    //   4. TTS reads the model's response sentence-by-sentence as
    //      tokens stream in (chunks at . ? ! to feel responsive)
    //   5. When TTS playback finishes, resume listening
    //   6. Esc / button click exits voice mode and stops everything
    //
    // Browser support: Web Speech API is Chrome/Edge/Safari only.
    // Firefox surfaces a friendly toast and doesn't enter the mode.

    let VOICE_STATE = {
        active: false,
        rec: null,            // SpeechRecognition instance
        stream: null,         // MediaStream from getUserMedia (kept for cleanup)
        overlay: null,        // DOM root for the voice overlay
        spokenChars: 0,       // index into respSlot.content already TTS'd
        ttsQueue: [],         // pending SpeechSynthesisUtterance chunks
        ttsSpeaking: false,
        silenceTimer: null,
        currentTranscript: "",
    };
    const VOICE_SILENCE_MS = 1200;   // pause length that ends an utterance

    async function enterVoiceMode() {
        const SR =
            window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SR || typeof window.speechSynthesis === "undefined") {
            showToast(
                "Voice mode needs Chrome, Edge, or Safari (Firefox doesn't support the Web Speech API yet)",
                { danger: true, holdMs: 4000 },
            );
            return;
        }
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            showToast(
                "Microphone access isn't available in this browser",
                { danger: true },
            );
            return;
        }
        // Ask the browser for mic permission up-front. SpeechRecognition
        // implicitly prompts too, but doing it explicitly via
        // getUserMedia gives us a cleaner failure path AND keeps the
        // mic stream open so the OS-level mic indicator stays on
        // throughout the session.
        try {
            VOICE_STATE.stream = await navigator.mediaDevices.getUserMedia({
                audio: true,
            });
        } catch (err) {
            const denied =
                err && (err.name === "NotAllowedError" || err.name === "PermissionDeniedError");
            showToast(
                denied
                    ? "Microphone access denied — allow it in your browser settings to use voice mode"
                    : "Couldn't access microphone: " + (err.message || err.name),
                { danger: true, holdMs: 4000 },
            );
            return;
        }
        VOICE_STATE.active = true;
        renderVoiceOverlay();
        startVoiceListening();
    }

    function exitVoiceMode() {
        VOICE_STATE.active = false;
        // Stop recognition
        if (VOICE_STATE.rec) {
            try {
                VOICE_STATE.rec.onend = null;
                VOICE_STATE.rec.stop();
            } catch (_) {}
            VOICE_STATE.rec = null;
        }
        // Stop any in-flight TTS
        if (window.speechSynthesis) {
            try {
                window.speechSynthesis.cancel();
            } catch (_) {}
        }
        VOICE_STATE.ttsQueue = [];
        VOICE_STATE.ttsSpeaking = false;
        VOICE_STATE.spokenChars = 0;
        VOICE_STATE.currentTranscript = "";
        if (VOICE_STATE.silenceTimer) {
            clearTimeout(VOICE_STATE.silenceTimer);
            VOICE_STATE.silenceTimer = null;
        }
        // Release the mic stream
        if (VOICE_STATE.stream) {
            VOICE_STATE.stream.getTracks().forEach((t) => {
                try { t.stop(); } catch (_) {}
            });
            VOICE_STATE.stream = null;
        }
        // Stop any inference streams that were started for voice mode
        stopAllStreams();
        // Drop the overlay
        if (VOICE_STATE.overlay) {
            VOICE_STATE.overlay.remove();
            VOICE_STATE.overlay = null;
        }
    }

    function renderVoiceOverlay() {
        if (VOICE_STATE.overlay) return;
        const overlay = document.createElement("div");
        overlay.className = "chat-voice-overlay";
        overlay.innerHTML =
            '<div class="chat-voice-backdrop"></div>' +
            '<div class="chat-voice-panel">' +
            '<button type="button" class="chat-voice-exit" aria-label="Exit voice mode">×</button>' +
            '<div class="chat-voice-orb" data-orb>' +
            '<div class="chat-voice-orb-pulse"></div>' +
            '<svg viewBox="0 0 32 32" width="48" height="48" fill="none" ' +
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
            'stroke-linejoin="round" aria-hidden="true">' +
            '<rect x="12" y="4" width="8" height="16" rx="4" />' +
            '<path d="M7 13 C7 18 11 22 16 22 C21 22 25 18 25 13" />' +
            '<path d="M16 22 L16 28" />' +
            '<path d="M12 28 L20 28" />' +
            "</svg></div>" +
            '<div class="chat-voice-status" data-voice-status>Listening…</div>' +
            '<div class="chat-voice-transcript" data-voice-transcript></div>' +
            '<div class="chat-voice-hint">Press <kbd>esc</kbd> to exit</div>' +
            "</div>";
        document.body.appendChild(overlay);
        overlay
            .querySelector(".chat-voice-exit")
            .addEventListener("click", exitVoiceMode);
        overlay
            .querySelector(".chat-voice-backdrop")
            .addEventListener("click", exitVoiceMode);
        VOICE_STATE.overlay = overlay;
        setVoiceState("listening", "Listening…");
    }

    function setVoiceState(stateClass, label) {
        if (!VOICE_STATE.overlay) return;
        const panel = VOICE_STATE.overlay.querySelector(".chat-voice-panel");
        panel.dataset.state = stateClass;
        const status = VOICE_STATE.overlay.querySelector("[data-voice-status]");
        if (status && label) status.textContent = label;
    }

    function setVoiceTranscript(text) {
        if (!VOICE_STATE.overlay) return;
        const el = VOICE_STATE.overlay.querySelector("[data-voice-transcript]");
        if (el) el.textContent = text || "";
    }

    function startVoiceListening() {
        const SR =
            window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SR || !VOICE_STATE.active) return;
        const rec = new SR();
        rec.continuous = true;
        rec.interimResults = true;
        rec.lang = "en-US";
        VOICE_STATE.rec = rec;
        VOICE_STATE.currentTranscript = "";
        setVoiceState("listening", "Listening…");

        rec.onresult = (e) => {
            let interim = "";
            let final = "";
            for (let i = e.resultIndex; i < e.results.length; i++) {
                const r = e.results[i];
                if (r.isFinal) {
                    final += r[0].transcript;
                } else {
                    interim += r[0].transcript;
                }
            }
            VOICE_STATE.currentTranscript = (
                VOICE_STATE.currentTranscript +
                " " +
                final
            ).trim();
            const display = (VOICE_STATE.currentTranscript + " " + interim).trim();
            setVoiceTranscript(display);
            // Reset the silence timer on every chunk — when audio
            // goes quiet for VOICE_SILENCE_MS we treat that as the
            // end of the user's utterance.
            if (VOICE_STATE.silenceTimer) clearTimeout(VOICE_STATE.silenceTimer);
            VOICE_STATE.silenceTimer = setTimeout(() => {
                finalizeUtterance();
            }, VOICE_SILENCE_MS);
        };
        rec.onerror = (e) => {
            // Some errors (no-speech) are benign; keep listening.
            if (e.error === "no-speech" || e.error === "audio-capture") {
                return;
            }
            console.warn("chat: voice rec error:", e.error);
        };
        rec.onend = () => {
            // If we're still active, restart so the loop persists.
            if (VOICE_STATE.active && !VOICE_STATE.ttsSpeaking) {
                try { rec.start(); } catch (_) {}
            }
        };
        try {
            rec.start();
        } catch (e) {
            console.warn("chat: voice rec start failed:", e);
        }
    }

    function finalizeUtterance() {
        if (!VOICE_STATE.active) return;
        const text = (VOICE_STATE.currentTranscript || "").trim();
        if (!text) return;
        VOICE_STATE.currentTranscript = "";
        setVoiceTranscript(text);
        setVoiceState("thinking", "Thinking…");
        // Pause the mic while the model thinks + speaks so TTS doesn't
        // feed back into STT.
        if (VOICE_STATE.rec) {
            try {
                VOICE_STATE.rec.onend = null;
                VOICE_STATE.rec.stop();
            } catch (_) {}
            VOICE_STATE.rec = null;
        }
        // Run the prompt through the standard send path; the voice
        // overlay's content-watcher (below) picks up streaming tokens
        // and feeds them to TTS.
        VOICE_STATE.spokenChars = 0;
        startVoiceSend(text);
    }

    async function startVoiceSend(text) {
        const input = document.querySelector("[data-chat-input]");
        if (input) input.value = text;
        // Use the existing send pipeline so chat history, multi-model,
        // and cost accounting all work the same as a typed message.
        const before = (getActiveChat() || { messages: [] }).messages.length;
        await handleSendClick();
        const chat = getActiveChat();
        if (!chat || chat.messages.length <= before) {
            setVoiceState("listening", "Listening…");
            startVoiceListening();
            return;
        }
        const assistantMsg = chat.messages[chat.messages.length - 1];
        setVoiceState("speaking", "Speaking…");
        // Poll for streaming tokens on the first response slot; chunk
        // at sentence boundaries and feed to TTS.
        const watcher = setInterval(() => {
            if (!VOICE_STATE.active) {
                clearInterval(watcher);
                return;
            }
            const resp = (assistantMsg.responses || [])[0];
            const content = (resp && resp.content) || "";
            const fresh = content.slice(VOICE_STATE.spokenChars);
            // Sentence boundary anywhere in the fresh slice → speak up
            // to and including the last terminator.
            const match = fresh.match(/^[\s\S]*[.!?](?=\s|$)/);
            if (match) {
                const chunk = match[0].trim();
                if (chunk) speakChunk(chunk);
                VOICE_STATE.spokenChars += match[0].length;
            }
            // Stream done → flush remaining text + resume listening.
            const streamDone =
                resp &&
                (resp.finish_reason ||
                    (resp.content && !STREAMS.size && content.length > 0));
            if (streamDone) {
                clearInterval(watcher);
                const tail = content.slice(VOICE_STATE.spokenChars).trim();
                if (tail) speakChunk(tail);
                // After the last chunk's onend, resumeListening kicks in.
                if (!VOICE_STATE.ttsQueue.length && !VOICE_STATE.ttsSpeaking) {
                    resumeListeningSoon();
                }
            }
        }, 120);
    }

    function speakChunk(text) {
        if (!VOICE_STATE.active || !window.speechSynthesis) return;
        const u = new SpeechSynthesisUtterance(text);
        u.rate = 1.05;
        u.pitch = 1.0;
        u.lang = "en-US";
        u.onend = () => {
            VOICE_STATE.ttsSpeaking = false;
            if (VOICE_STATE.ttsQueue.length) {
                const next = VOICE_STATE.ttsQueue.shift();
                VOICE_STATE.ttsSpeaking = true;
                window.speechSynthesis.speak(next);
            } else if (VOICE_STATE.active && !STREAMS.size) {
                resumeListeningSoon();
            }
        };
        if (VOICE_STATE.ttsSpeaking) {
            VOICE_STATE.ttsQueue.push(u);
        } else {
            VOICE_STATE.ttsSpeaking = true;
            window.speechSynthesis.speak(u);
        }
    }

    function resumeListeningSoon() {
        if (!VOICE_STATE.active) return;
        // Short beat so the user has a half-second to start speaking
        // without the mic picking up the tail of TTS.
        setTimeout(() => {
            if (VOICE_STATE.active && !VOICE_STATE.ttsSpeaking) {
                startVoiceListening();
            }
        }, 350);
    }

    // ── Voice input (one-shot Web Speech API — push-to-talk) ──────────
    // The original "click mic to dictate a single prompt" flow. Kept
    // alongside the conversational Voice Mode above so users have a
    // quick option without entering the full overlay.

    function startVoiceInput() {
        const SR =
            window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SR) {
            showToast("Voice input isn't supported in this browser", {
                danger: true,
            });
            return;
        }
        const rec = new SR();
        rec.continuous = false;
        rec.interimResults = false;
        rec.lang = "en-US";
        const input = document.querySelector("[data-chat-input]");
        const btn = document.querySelector("[data-chat-voice]");
        if (btn) btn.classList.add("is-recording");
        rec.onresult = (e) => {
            const transcript = Array.from(e.results)
                .map((r) => r[0].transcript)
                .join(" ");
            if (input) {
                const existing = input.value || "";
                input.value = (existing ? existing + " " : "") + transcript;
                autoResize(input);
                updateInputEstimate();
                input.focus();
            }
        };
        rec.onend = () => {
            if (btn) btn.classList.remove("is-recording");
        };
        rec.onerror = () => {
            if (btn) btn.classList.remove("is-recording");
        };
        try {
            rec.start();
        } catch (e) {
            console.warn("chat: voice rec start failed:", e);
            if (btn) btn.classList.remove("is-recording");
        }
    }

    // ── File attachments (image input for vision models) ──────────────

    async function attachFileToInput(file) {
        if (!file) return;
        if (!file.type.startsWith("image/")) {
            showToast("Only image files are supported", { danger: true });
            return;
        }
        const dataUrl = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
        const chat = ensureActiveChat();
        if (!chat._pending_attachments) chat._pending_attachments = [];
        chat._pending_attachments.push({
            type: "image_url",
            image_url: { url: dataUrl },
        });
        renderAttachmentTray();
    }

    function renderAttachmentTray() {
        const tray = document.querySelector("[data-chat-attachments]");
        if (!tray) return;
        const chat = getActiveChat();
        const pending = (chat && chat._pending_attachments) || [];
        tray.innerHTML = "";
        if (pending.length === 0) {
            tray.hidden = true;
            return;
        }
        tray.hidden = false;
        pending.forEach((att, idx) => {
            const thumb = document.createElement("div");
            thumb.className = "chat-attachment-thumb";
            if (att.type === "image_url") {
                const img = document.createElement("img");
                img.src = att.image_url.url;
                thumb.appendChild(img);
            }
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "chat-attachment-remove";
            remove.textContent = "×";
            remove.addEventListener("click", () => {
                chat._pending_attachments.splice(idx, 1);
                renderAttachmentTray();
            });
            thumb.appendChild(remove);
            tray.appendChild(thumb);
        });
    }

    function consumePendingAttachments(chat) {
        const a = chat._pending_attachments || [];
        chat._pending_attachments = [];
        renderAttachmentTray();
        return a;
    }

    // ── In-chat search (Cmd/Ctrl+F) ──────────────────────────────────
    // Floating search bar above the input filters which messages render
    // in the active chat. Matches stay; non-matching messages are
    // hidden via CSS. Matched terms get highlighted with a <mark>.

    let chatFilterQuery = "";

    function openChatSearch() {
        let bar = document.querySelector(".chat-search-bar");
        if (bar) {
            const input = bar.querySelector("input");
            if (input) input.focus();
            return;
        }
        bar = document.createElement("div");
        bar.className = "chat-search-bar";
        bar.innerHTML =
            '<input type="search" placeholder="Find in this chat…" autofocus>' +
            '<span class="chat-search-count"></span>' +
            '<button type="button" class="chat-search-close" aria-label="Close">×</button>';
        const main = document.querySelector(".chat-main");
        if (main) main.appendChild(bar);
        const input = bar.querySelector("input");
        input.value = chatFilterQuery;
        input.addEventListener("input", () => {
            chatFilterQuery = input.value;
            renderThread();
            updateSearchCount();
        });
        input.addEventListener("keydown", (e) => {
            if (e.key === "Escape") closeChatSearch();
        });
        bar.querySelector(".chat-search-close").addEventListener(
            "click",
            closeChatSearch,
        );
        updateSearchCount();
    }

    function closeChatSearch() {
        const bar = document.querySelector(".chat-search-bar");
        if (bar) bar.remove();
        chatFilterQuery = "";
        renderThread();
    }

    function updateSearchCount() {
        const bar = document.querySelector(".chat-search-bar");
        if (!bar) return;
        const count = bar.querySelector(".chat-search-count");
        if (!count) return;
        const matches = document.querySelectorAll(".chat-msg.is-match").length;
        count.textContent = chatFilterQuery
            ? matches + " match" + (matches === 1 ? "" : "es")
            : "";
    }

    function messageMatchesSearch(msg) {
        if (!chatFilterQuery) return true;
        const q = chatFilterQuery.toLowerCase();
        if (msg.role === "user") {
            return (msg.content || "").toLowerCase().includes(q);
        }
        for (const r of msg.responses || []) {
            if ((r.content || "").toLowerCase().includes(q)) return true;
        }
        return false;
    }

    // ── Keyboard shortcuts ────────────────────────────────────────────

    function handleGlobalShortcut(e) {
        // Skip if inside an input/textarea (except specific combos)
        const targetTag =
            e.target && e.target.tagName ? e.target.tagName.toLowerCase() : "";
        const inField = targetTag === "input" || targetTag === "textarea";
        // Esc — exit voice mode first if active, else stop any active
        // stream (before falling through to close-modal Esc behavior).
        if (e.key === "Escape" && VOICE_STATE.active) {
            e.preventDefault();
            exitVoiceMode();
            return;
        }
        if (e.key === "Escape" && STREAMS.size > 0) {
            e.preventDefault();
            stopAllStreams();
            return;
        }
        // Cmd/Ctrl+Enter — send (handled by the input handler too, but
        // also works when the input has focus)
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            const sendBtn = document.querySelector("[data-chat-send]");
            if (sendBtn) sendBtn.click();
            return;
        }
        // Cmd/Ctrl+N — new chat
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") {
            e.preventDefault();
            newChat();
            return;
        }
        // Cmd/Ctrl+E — export current chat to JSON
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "e") {
            e.preventDefault();
            exportChatJSON();
            return;
        }
        // Cmd/Ctrl+F — in-chat search bar
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") {
            e.preventDefault();
            openChatSearch();
            return;
        }
        // Cmd/Ctrl + J / K — next / prev chat in sidebar (works even
        // while focused in the input)
        if (
            (e.metaKey || e.ctrlKey) &&
            (e.key.toLowerCase() === "j" || e.key.toLowerCase() === "k")
        ) {
            e.preventDefault();
            const dir = e.key.toLowerCase() === "j" ? 1 : -1;
            const chats = Object.values(STATE.chats).sort((a, b) => {
                const pa = a.pinned ? 1 : 0;
                const pb = b.pinned ? 1 : 0;
                if (pa !== pb) return pb - pa;
                return new Date(b.updated_at) - new Date(a.updated_at);
            });
            const idx = chats.findIndex((c) => c.id === STATE.activeChatId);
            const next = Math.max(0, Math.min(chats.length - 1, idx + dir));
            if (chats[next]) setActiveChat(chats[next].id);
            return;
        }
        if (inField) return;
        // / — focus new chat (no modifier)
        if (e.key === "/") {
            e.preventDefault();
            const input = document.querySelector("[data-chat-input]");
            if (input) input.focus();
            return;
        }
        // K — open Add Model
        if (e.key.toLowerCase() === "k") {
            e.preventDefault();
            addModel();
            return;
        }
        // V — enter voice mode
        if (e.key.toLowerCase() === "v") {
            e.preventDefault();
            enterVoiceMode();
            return;
        }
        // ? — open shortcuts help
        if (e.key === "?" || (e.key === "/" && e.shiftKey)) {
            e.preventDefault();
            showShortcutsHelp();
        }
    }

    function showSettings() {
        const overlay = document.createElement("div");
        overlay.className = "chat-settings-overlay";
        const prefs = STATE.preferences;
        const enterToSend = prefs.enter_to_send !== false; // default true
        overlay.innerHTML =
            '<div class="chat-settings-backdrop" data-close></div>' +
            '<div class="chat-settings-panel">' +
            '<div class="chat-settings-head">' +
            '<h3>Settings</h3>' +
            '<button class="chat-settings-close" type="button" data-close>×</button>' +
            "</div>" +
            '<div class="chat-settings-body">' +
            '<label class="chat-settings-row">' +
            '<span class="chat-settings-row-label">Default system prompt</span>' +
            '<textarea data-setting="default_system_prompt" rows="3" placeholder="Used when a new chat doesn\'t set one.">' +
            escapeHtml(prefs.defaultSystemPrompt || "") +
            "</textarea>" +
            "</label>" +
            '<label class="chat-settings-row">' +
            '<span class="chat-settings-row-label">Default model</span>' +
            '<input type="text" data-setting="default_model_id" value="' +
            escapeHtml(prefs.lastModelId || "") +
            '" placeholder="anthropic/claude-sonnet-4.6">' +
            "</label>" +
            '<label class="chat-settings-row chat-settings-row-flex">' +
            '<input type="checkbox" data-setting="enter_to_send" ' +
            (enterToSend ? "checked" : "") +
            ">" +
            '<span>Press Enter to send (Shift+Enter for newline). Disable for newline-only Enter.</span>' +
            "</label>" +
            '<div class="chat-settings-row">' +
            '<span class="chat-settings-row-label">Saved presets</span>' +
            '<div class="chat-settings-presets">' +
            (prefs.presets || [])
                .map(
                    (p, i) =>
                        '<div class="chat-settings-preset">' +
                        '<span>' +
                        escapeHtml(p.name) +
                        "</span>" +
                        '<button type="button" data-delete-preset="' +
                        i +
                        '">×</button>' +
                        "</div>",
                )
                .join("") +
            ((prefs.presets || []).length === 0
                ? '<span class="chat-settings-empty">Save your first preset from the per-model dropdown.</span>'
                : "") +
            "</div></div>" +
            '<div class="chat-settings-row">' +
            '<span class="chat-settings-row-label">Wipe all data</span>' +
            '<button class="chat-settings-danger" type="button" data-wipe>Clear all chats + preferences</button>' +
            "</div>" +
            "</div>" +
            "</div>";
        document.body.appendChild(overlay);
        overlay.addEventListener("click", (e) => {
            const target = e.target;
            if (target && target.dataset && target.dataset.close != null) {
                overlay.remove();
                return;
            }
            if (target && target.dataset && target.dataset.deletePreset != null) {
                const i = parseInt(target.dataset.deletePreset, 10);
                if (prefs.presets) prefs.presets.splice(i, 1);
                saveState();
                overlay.remove();
                showSettings();
                return;
            }
            if (target && target.dataset && target.dataset.wipe != null) {
                confirmModal({
                    title: "Clear all chats and preferences?",
                    message: "Every chat and preference on this device will be removed. This can't be undone.",
                    confirmText: "Clear everything",
                    danger: true,
                }).then((ok) => {
                    if (!ok) return;
                    STATE = { chats: {}, activeChatId: null, preferences: {} };
                    saveState();
                    overlay.remove();
                    ensureActiveChat();
                    renderSidebar();
                    renderModelsBar();
                    renderSystemPrompt();
                    renderThread();
                    showToast("Cleared");
                });
            }
        });
        overlay.addEventListener("change", (e) => {
            const target = e.target;
            if (!target.dataset || !target.dataset.setting) return;
            const key = target.dataset.setting;
            if (target.type === "checkbox") {
                prefs[key] = target.checked;
            } else {
                prefs[key] = target.value;
            }
            saveState();
        });
        overlay.addEventListener("input", (e) => {
            const target = e.target;
            if (!target.dataset || !target.dataset.setting) return;
            const key = target.dataset.setting;
            if (key === "default_system_prompt") {
                prefs.defaultSystemPrompt = target.value;
            } else if (key === "default_model_id") {
                prefs.lastModelId = target.value;
            }
            saveState();
        });
    }

    function showShortcutsHelp() {
        const html =
            '<div class="chat-help-overlay" data-close>' +
            '<div class="chat-help-panel">' +
            "<h3>Keyboard shortcuts</h3>" +
            "<table>" +
            "<tr><td><kbd>⌘/Ctrl + Enter</kbd></td><td>Send message</td></tr>" +
            "<tr><td><kbd>⌘/Ctrl + N</kbd></td><td>New chat</td></tr>" +
            "<tr><td><kbd>⌘/Ctrl + E</kbd></td><td>Export to JSON</td></tr>" +
            "<tr><td><kbd>⌘/Ctrl + J / K</kbd></td><td>Next / previous chat</td></tr>" +
            "<tr><td><kbd>/</kbd></td><td>Focus input</td></tr>" +
            "<tr><td><kbd>K</kbd></td><td>Add model</td></tr>" +
            "<tr><td><kbd>V</kbd></td><td>Enter voice mode</td></tr>" +
            "<tr><td><kbd>Esc</kbd></td><td>Stop streams / exit voice / close menu</td></tr>" +
            "<tr><td><kbd>?</kbd></td><td>Show this help</td></tr>" +
            "</table>" +
            "<p>Click outside to close.</p>" +
            "</div></div>";
        const el = document.createElement("div");
        el.innerHTML = html;
        const node = el.firstChild;
        document.body.appendChild(node);
        node.addEventListener("click", (e) => {
            if (e.target.dataset && e.target.dataset.close != null) {
                node.remove();
            }
        });
    }

    // ── Init ──────────────────────────────────────────────────────────

    function init() {
        bootstrapBrowserKey();
        ensureActiveChat();
        importSharedChatFromHash();
        renderSidebar();
        renderModelsBar();
        renderSystemPrompt();
        renderThread();
        renderAttachmentTray();
        loadModels();
        updateInputEstimate();

        // Live-update the sidebar's relative timestamps every minute
        // so "1m" doesn't sit there reading "1m" for an hour. Only
        // touches the time spans (cheap render) — avoids a full
        // sidebar re-render so scroll position is preserved.
        setInterval(() => {
            document.querySelectorAll(".chat-sidebar-item").forEach((el) => {
                const chatId = el.dataset.chatId;
                const chat = chatId && STATE.chats[chatId];
                if (!chat) return;
                const time = el.querySelector(".chat-sidebar-title-time");
                if (time) time.textContent = relativeTime(chat.updated_at);
            });
        }, 60_000);

        const sendBtn = document.querySelector("[data-chat-send]");
        if (sendBtn) sendBtn.addEventListener("click", handleSendClick);

        const input = document.querySelector("[data-chat-input]");
        if (input) {
            input.addEventListener("input", () => {
                autoResize(input);
                updateInputEstimate();
            });
            input.addEventListener("keydown", (e) => {
                if (
                    (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ||
                    (e.key === "Enter" && !e.shiftKey && window.innerWidth >= 780)
                ) {
                    e.preventDefault();
                    handleSendClick(e);
                }
            });
            // Drag-and-drop image attachments — drop directly on the
            // input or anywhere within the main pane.
            const main = document.querySelector(".chat-main");
            if (main) {
                main.addEventListener("dragover", (e) => {
                    e.preventDefault();
                    main.classList.add("is-dragover");
                });
                main.addEventListener("dragleave", () => {
                    main.classList.remove("is-dragover");
                });
                main.addEventListener("drop", (e) => {
                    e.preventDefault();
                    main.classList.remove("is-dragover");
                    const files = e.dataTransfer && e.dataTransfer.files;
                    if (files && files.length > 0) {
                        for (const f of files) attachFileToInput(f);
                    }
                });
            }
        }

        document.addEventListener("keydown", handleGlobalShortcut);

        // Scroll-to-bottom FAB visibility tied to thread scroll.
        const thread = document.querySelector("[data-chat-thread]");
        if (thread) {
            thread.addEventListener(
                "scroll",
                () => updateScrollToBottomVisibility(),
                { passive: true },
            );
        }
        const fab = document.querySelector("[data-chat-scroll-fab]");
        if (fab) {
            fab.addEventListener("click", () => {
                const t = document.querySelector("[data-chat-thread]");
                if (!t) return;
                if (typeof t.scrollTo === "function") {
                    t.scrollTo({ top: t.scrollHeight, behavior: "smooth" });
                } else {
                    t.scrollTop = t.scrollHeight;
                }
            });
        }

        // Sidebar search
        const sbSearch = document.querySelector("[data-chat-sidebar-search]");
        if (sbSearch) {
            sbSearch.addEventListener("input", () => {
                SIDEBAR_QUERY = sbSearch.value;
                renderSidebar();
            });
        }

        // Mobile sidebar backdrop tap-to-close
        const backdrop = document.querySelector("[data-chat-sidebar-backdrop]");
        if (backdrop) {
            backdrop.addEventListener("click", () => {
                const sidebar = document.querySelector("[data-chat-sidebar]");
                if (sidebar) {
                    sidebar.dataset.open = "false";
                    backdrop.hidden = true;
                }
            });
        }

        // Window unload — clean any sessionStorage we don't need to
        // persist past the tab close. Browser keys + state stay so the
        // user resumes where they left off.
        // (intentionally not clearing sessionStorage here; key is
        // session-scoped already and clears naturally on tab close)

        document.addEventListener("click", (e) => {
            const target = e.target;
            if (!target || !target.closest) return;
            // Toggle a model pill's dropdown — separate from picker.
            // The picker is opened from inside the dropdown via the
            // "Change model" action.
            const pillToggle = target.closest('[data-action="toggle-model-dropdown"]');
            if (pillToggle) {
                const slotIdx = parseInt(pillToggle.dataset.slotIdx || "0", 10);
                toggleModelDropdown(slotIdx);
                return;
            }
            const opener = target.closest('[data-action="open-model-picker"]');
            if (opener) {
                const slotIdx = opener.dataset.slotIdx != null
                    ? parseInt(opener.dataset.slotIdx, 10)
                    : 0;
                openModelPicker(slotIdx);
                return;
            }
            if (target.closest('[data-action="add-model"]')) {
                addModel();
                return;
            }
            const dupe = target.closest('[data-action="duplicate-model"]');
            if (dupe) {
                duplicateModel(parseInt(dupe.dataset.slotIdx, 10));
                return;
            }
            const rm = target.closest('[data-action="remove-model"]');
            if (rm) {
                removeModel(parseInt(rm.dataset.slotIdx, 10));
                return;
            }
            const loadP = target.closest('[data-action="load-preset"]');
            if (loadP) {
                loadPreset(
                    parseInt(loadP.dataset.slotIdx, 10),
                    loadP.dataset.presetName,
                );
                return;
            }
            const saveP = target.closest('[data-action="save-preset"]');
            if (saveP) {
                const slotIdx = parseInt(saveP.dataset.slotIdx, 10);
                promptModal({
                    title: "Save preset",
                    label: "Preset name",
                    placeholder: "e.g. Creative, Strict, Long output",
                    confirmText: "Save",
                }).then((name) => {
                    if (!name || !name.trim()) return;
                    const chat = ensureActiveChat();
                    const slot = chat.models[slotIdx];
                    if (slot) {
                        savePreset(name.trim(), slot);
                        renderModelsBar();
                        showToast("Preset saved");
                    }
                });
                return;
            }
            const newChatBtn = target.closest('[data-action="new-chat"]');
            if (newChatBtn) {
                newChat();
                return;
            }
            const toggle = target.closest('[data-action="toggle-system-prompt"]');
            if (toggle) {
                const panel = document.querySelector("[data-chat-system-prompt]");
                if (panel) panel.hidden = !panel.hidden;
                return;
            }
            if (target.closest('[data-action="clear-chat"]')) {
                clearCurrentChat();
                return;
            }
            const hamburger = target.closest('[data-action="toggle-sidebar"]');
            if (hamburger) {
                const sidebar = document.querySelector("[data-chat-sidebar]");
                const bd = document.querySelector("[data-chat-sidebar-backdrop]");
                if (sidebar) {
                    const open = sidebar.dataset.open === "true";
                    sidebar.dataset.open = open ? "false" : "true";
                    if (bd) bd.hidden = open;
                }
                return;
            }
            // Chunk 4 actions
            if (target.closest('[data-action="export-json"]')) {
                exportChatJSON();
                return;
            }
            if (target.closest('[data-action="export-md"]')) {
                exportChatMarkdown();
                return;
            }
            if (target.closest('[data-action="share-link"]')) {
                exportShareLink();
                return;
            }
            if (target.closest('[data-action="attach-file"]')) {
                const f = document.querySelector("[data-chat-file-input]");
                if (f) f.click();
                return;
            }
            if (target.closest('[data-action="voice-input"]')) {
                startVoiceInput();
                return;
            }
            if (target.closest('[data-action="enter-voice-mode"]')) {
                enterVoiceMode();
                return;
            }
            if (target.closest('[data-action="show-shortcuts"]')) {
                showShortcutsHelp();
                return;
            }
            if (target.closest('[data-action="show-settings"]')) {
                showSettings();
                return;
            }
            // Rename via double-click in sidebar — wired separately via
            // dblclick on .chat-sidebar-title; bare clicks just select.

            // Click outside any pill or dropdown closes the open dropdown.
            if (openDropdownSlotIdx >= 0 && !target.closest(".chat-model-pill-wrap")) {
                openDropdownSlotIdx = -1;
                renderModelsBar();
            }
        });

        document.addEventListener("dblclick", (e) => {
            const titleBtn = e.target && e.target.closest
                ? e.target.closest(".chat-sidebar-title")
                : null;
            if (titleBtn) {
                const item = titleBtn.closest("[data-chat-id]");
                if (item) renameChatPrompt(item.dataset.chatId);
            }
        });

        document.addEventListener("change", (e) => {
            const fileInput = e.target && e.target.closest
                ? e.target.closest("[data-chat-file-input]")
                : null;
            if (fileInput && fileInput.files) {
                for (const f of fileInput.files) attachFileToInput(f);
                fileInput.value = ""; // allow re-selecting the same file
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
