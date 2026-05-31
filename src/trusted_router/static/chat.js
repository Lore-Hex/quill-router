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
 *     and fire ZERO requests to api.quillrouter.com.
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
    const STORAGE_KEY = "tr_chat_state_v1";
    const KEY_SESSION_STORAGE = "tr_chat_key";
    const KEY_COOKIE =
        (window.__TR_CHAT__ && window.__TR_CHAT__.keyCookieName) || "tr_chat_key";
    const API_BASE =
        (window.__TR_CHAT__ && window.__TR_CHAT__.apiBaseUrl) ||
        "https://api.quillrouter.com/v1";
    const ISSUE_KEY_PATH =
        (window.__TR_CHAT__ && window.__TR_CHAT__.issueKeyPath) ||
        "/internal/chat/issue-browser-key";
    const DEFAULT_MODEL_ID = "anthropic/claude-sonnet-4.6";
    const MAX_MODELS_PER_CHAT = 4; // matches OpenRouter's apparent cap
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
                    parsed.chats = parsed.chats || {};
                    parsed.preferences = parsed.preferences || {};
                    return parsed;
                }
            }
        } catch (_) {
            // Corrupt state — reset rather than break the page
        }
        return { chats: {}, activeChatId: null, preferences: {} };
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
    async function ensureBrowserKey() {
        let key = null;
        try {
            key = sessionStorage.getItem(KEY_SESSION_STORAGE);
        } catch (_) {}
        if (key) return key;

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
            const resp = await fetch(API_BASE + "/models");
            if (!resp.ok) throw new Error("models fetch " + resp.status);
            const json = await resp.json();
            const data = Array.isArray(json.data) ? json.data : [];
            // Each model has top-level id + an OpenRouter-shaped
            // pricing block; TR also surfaces a `trustedrouter`
            // extension with provider-specific context.
            MODELS = data.map((m) => normalizeModel(m));
            renderModelPicker();
        } catch (e) {
            console.warn("chat: model catalog load failed:", e);
        } finally {
            MODELS_LOADING = false;
        }
    }

    function normalizeModel(raw) {
        const pricing = raw.pricing || {};
        // Pricing in OpenAI shape is dollars per token; convert to
        // $/M for display.
        const inPerM =
            pricing.prompt != null ? Number(pricing.prompt) * 1_000_000 : null;
        const outPerM =
            pricing.completion != null
                ? Number(pricing.completion) * 1_000_000
                : null;
        const ext = raw.trustedrouter || {};
        return {
            id: raw.id,
            name: raw.name || raw.id,
            description: raw.description || "",
            context_length: raw.context_length || ext.context_length || null,
            input_per_m: inPerM,
            output_per_m: outPerM,
            uptime_pct: ext.uptime_pct || null,
            capabilities: ext.capabilities || [],
            free: pricing && Number(pricing.prompt) === 0,
        };
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
            title: "New chat",
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
            title.innerHTML =
                '<span class="chat-sidebar-title-text">' +
                escapeHtml(chat.title) +
                "</span>" +
                '<span class="chat-sidebar-title-time">' +
                escapeHtml(relativeTime(chat.updated_at)) +
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
                if (confirm("Delete this chat?")) deleteChat(chat.id);
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

        chat.models.forEach((slot, idx) => {
            bar.appendChild(makeModelPill(chat, slot, idx));
        });

        if (chat.models.length < MAX_MODELS_PER_CHAT) {
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
        if (costEl) {
            if (!chat) {
                costEl.textContent = "";
                return;
            }
            let totalCents = 0;
            let totalIn = 0;
            let totalOut = 0;
            for (const m of chat.messages || []) {
                if (m.role !== "assistant") continue;
                for (const r of m.responses || []) {
                    totalCents += (r.cost_microdollars || 0) / 10_000;
                    totalIn += r.tokens_in || 0;
                    totalOut += r.tokens_out || 0;
                }
            }
            if (totalCents === 0 && totalIn === 0 && totalOut === 0) {
                costEl.textContent = "";
            } else {
                costEl.textContent =
                    "$" + (totalCents / 100).toFixed(4) +
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
        pill.dataset.action = "toggle-model-dropdown";
        pill.dataset.slotIdx = String(idx);
        const label =
            slot.label ||
            (model && model.name) ||
            slot.model_id ||
            "Select a model";
        const provider = providerFromModelId(slot.model_id);
        const avatar = providerAvatar(provider, "chat-avatar-pill");
        pill.innerHTML =
            avatar +
            '<span class="chat-model-pill-name">' +
            escapeHtml(label) +
            "</span>" +
            (chat.models.length > 1
                ? '<span class="chat-model-pill-num">#' + (idx + 1) + "</span>"
                : "") +
            '<span class="chat-model-pill-caret">▾</span>';
        wrap.appendChild(pill);
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
            (chat.models.length > 1
                ? '<button type="button" class="chat-dd-action chat-dd-action-danger" data-action="remove-model" data-slot-idx="' +
                  idx +
                  '">Remove</button>'
                : "") +
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
        const chat = ensureActiveChat();
        if (chat.models.length <= 1) return;
        chat.models.splice(idx, 1);
        openDropdownSlotIdx = -1;
        chat.updated_at = isoNow();
        saveState();
        renderModelsBar();
        renderThread();
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
        };
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
            : '<div class="chat-welcome">' +
              '<button class="chat-welcome-close" data-action="dismiss-welcome" aria-label="Dismiss">×</button>' +
              '<div class="chat-welcome-eyebrow">Welcome</div>' +
              '<h3>Compare models side-by-side</h3>' +
              '<ol>' +
              '<li>Pick a model in the header, type a prompt.</li>' +
              '<li>Hit <kbd>+ Add model</kbd> to add up to 3 more. Each one streams its response in its own column.</li>' +
              '<li>Sign in only when you press Send — nothing fires until then.</li>' +
              "</ol>" +
              "</div>";
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
        empty.innerHTML =
            welcomeBanner +
            '<h2>Try any model — zero tokens until you sign in.</h2>' +
            '<p>Pick a model above, type a prompt, hit Send. Compare up to 4 models side-by-side.</p>' +
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

    function renderThread() {
        const thread = document.querySelector("[data-chat-thread]");
        if (!thread) return;
        const chat = ensureActiveChat();
        thread.innerHTML = "";
        if (chat.messages.length === 0) {
            renderEmptyState(thread);
            return;
        }
        for (const msg of chat.messages) {
            thread.appendChild(renderMessage(msg, chat));
        }
        thread.scrollTop = thread.scrollHeight;
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
                const cents = (finalMicro || estMicro) / 10_000;
                const costStr =
                    finalMicro > 0
                        ? "$" + (cents / 100).toFixed(4)
                        : estMicro > 0
                            ? "~$" + (cents / 100).toFixed(4)
                            : "$0.0000";
                const tps = resp.tokens_per_sec
                    ? "  ·  " + resp.tokens_per_sec + " t/s"
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
                        : "");
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
    function showToast(text) {
        let toast = document.querySelector(".chat-toast");
        if (!toast) {
            toast = document.createElement("div");
            toast.className = "chat-toast";
            document.body.appendChild(toast);
        }
        toast.textContent = text;
        toast.classList.add("is-visible");
        if (_toastTimer) clearTimeout(_toastTimer);
        _toastTimer = setTimeout(() => {
            toast.classList.remove("is-visible");
        }, 1800);
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
    const PICKER_FILTERS = { free: false, vision: false, tools: false };

    function renderModelPicker() {
        if (!pickerEl) return;
        const list = pickerEl.querySelector(".chat-model-picker-list");
        if (!list) return;
        list.innerHTML = "";
        const q = pickerQuery.toLowerCase();
        const filtered = MODELS.filter((m) => {
            if (
                q &&
                !m.id.toLowerCase().includes(q) &&
                !(m.name || "").toLowerCase().includes(q)
            ) {
                return false;
            }
            const caps = m.capabilities || [];
            if (PICKER_FILTERS.free && !m.free) return false;
            if (PICKER_FILTERS.vision && !caps.includes("vision")) return false;
            if (
                PICKER_FILTERS.tools &&
                !caps.includes("tools") &&
                !caps.includes("tool_use")
            )
                return false;
            return true;
        }).slice(0, 200);
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
                }
            }
        }
        // Group by provider so the list reads as anthropic | openai |
        // google sections instead of a flat alphabetical jumble.
        const grouped = new Map();
        for (const m of filtered) {
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
        pickerTargetSlot = typeof slotIdx === "number" ? slotIdx : 0;
        if (pickerEl) return;
        pickerEl = document.createElement("div");
        pickerEl.className = "chat-model-picker";
        pickerEl.innerHTML = `
            <div class="chat-model-picker-backdrop" data-close></div>
            <div class="chat-model-picker-panel">
                <input type="text" class="chat-model-picker-search" placeholder="Search models..." autofocus>
                <div class="chat-model-picker-filters">
                    <button type="button" class="chat-picker-filter" data-filter="free">Free</button>
                    <button type="button" class="chat-picker-filter" data-filter="vision">Vision</button>
                    <button type="button" class="chat-picker-filter" data-filter="tools">Tools</button>
                </div>
                <div class="chat-model-picker-list"></div>
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
        // Render loading skeleton if the catalog isn't loaded yet,
        // otherwise show rows.
        if (MODELS.length === 0 && MODELS_LOADING) {
            const list = pickerEl.querySelector(".chat-model-picker-list");
            for (let i = 0; i < 6; i++) {
                const sk = document.createElement("div");
                sk.className = "chat-model-skeleton";
                list.appendChild(sk);
            }
        } else {
            renderModelPicker();
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
                        if (r) r.error = String(e && e.message ? e.message : e);
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
        const resp = await fetch(API_BASE + "/chat/completions", {
            method: "POST",
            signal: abort.signal,
            headers: {
                "Content-Type": "application/json",
                Authorization: "Bearer " + key,
            },
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            const errText = await resp.text();
            throw new Error(errText.slice(0, 240));
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        const respSlot = assistantMsg.responses[respIdx] || assistantMsg.responses[0];
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
    function updateSendButtonMode() {
        const btn = document.querySelector("[data-chat-send]");
        if (!btn) return;
        if (STREAMS.size > 0) {
            btn.innerHTML = 'Stop <kbd class="chat-send-kbd">esc</kbd>';
            btn.dataset.mode = "stop";
            btn.classList.add("is-stop");
        } else {
            btn.innerHTML = 'Send <kbd class="chat-send-kbd">⌘↵</kbd>';
            btn.dataset.mode = "send";
            btn.classList.remove("is-stop");
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
    // Rough char-to-token ratio: ~4 chars per token for English. Good
    // enough for an order-of-magnitude estimate. Precise cost lands
    // when the response comes back.

    function approxTokens(text) {
        if (!text) return 0;
        return Math.ceil(text.length / 4);
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
            const estCents = (tokens * totalRate) / 1_000_000 * 100;
            if (tokens > 0 && totalRate > 0) {
                cCounter.textContent =
                    "~$" + (estCents / 100).toFixed(4) +
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
                        const cents = (r.cost_microdollars || 0) / 10_000;
                        lines.push("");
                        lines.push(
                            "_$" +
                                (cents / 100).toFixed(4) +
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
            alert(
                "This chat is too large to share via URL (" +
                    url.length +
                    " chars). Use Export to JSON instead.",
            );
            return null;
        }
        navigator.clipboard.writeText(url).then(
            () => alert("Share link copied to clipboard."),
            () => prompt("Copy this share link:", url),
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
        const next = prompt("Rename chat:", chat.title || "");
        if (next == null) return;
        chat.title = next.trim() || chat.title || "Untitled";
        chat.updated_at = isoNow();
        saveState();
        renderSidebar();
    }

    // ── Voice input (Web Speech API) ──────────────────────────────────

    function startVoiceInput() {
        const SR =
            window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SR) {
            alert("Voice input isn't supported in this browser.");
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
            alert("Only image files are supported in V1.");
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
        // Esc — stop any active stream first (before falling through to
        // close-modal Esc behavior elsewhere).
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
                if (confirm("Clear ALL chats and preferences? This can't be undone.")) {
                    STATE = { chats: {}, activeChatId: null, preferences: {} };
                    saveState();
                    overlay.remove();
                    ensureActiveChat();
                    renderSidebar();
                    renderModelsBar();
                    renderSystemPrompt();
                    renderThread();
                }
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
            "<tr><td><kbd>Esc</kbd></td><td>Stop streams / close menu</td></tr>" +
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
                const name = prompt("Name this preset:");
                if (name && name.trim()) {
                    const chat = ensureActiveChat();
                    const slot = chat.models[parseInt(saveP.dataset.slotIdx, 10)];
                    if (slot) {
                        savePreset(name.trim(), slot);
                        renderModelsBar();
                    }
                }
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
