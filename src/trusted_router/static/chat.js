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

    function renderSidebar() {
        const list = document.querySelector("[data-chat-list]");
        if (!list) return;
        list.innerHTML = "";
        const chats = Object.values(STATE.chats).sort(
            (a, b) => new Date(b.updated_at) - new Date(a.updated_at),
        );
        if (chats.length === 0) {
            const empty = document.createElement("div");
            empty.className = "chat-sidebar-empty";
            empty.textContent = "No chats yet.";
            list.appendChild(empty);
            return;
        }
        let lastBucket = "";
        for (const chat of chats) {
            const bucket = dateBucket(chat.updated_at);
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
            title.textContent = chat.title;
            title.addEventListener("click", () => setActiveChat(chat.id));
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
        pill.innerHTML =
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
            if (!target || target.dataset.action !== "toggle-enabled") return;
            const slotIdx = parseInt(target.dataset.slotIdx, 10);
            const targetSlot = chat.models[slotIdx];
            if (!targetSlot) return;
            targetSlot.enabled = target.checked;
            chat.updated_at = isoNow();
            saveState();
            renderModelsBar();
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

    function renderThread() {
        const thread = document.querySelector("[data-chat-thread]");
        if (!thread) return;
        const chat = ensureActiveChat();
        thread.innerHTML = "";
        if (chat.messages.length === 0) {
            const empty = document.createElement("div");
            empty.className = "chat-empty";
            empty.innerHTML =
                '<h2>Try any model — zero tokens until you sign in.</h2>' +
                '<p>Pick a model above, type a prompt, hit Send. Sign in to actually run it.</p>';
            thread.appendChild(empty);
            return;
        }
        for (const msg of chat.messages) {
            thread.appendChild(renderMessage(msg, chat));
        }
        thread.scrollTop = thread.scrollHeight;
    }

    function renderMessage(msg, chat) {
        const el = document.createElement("div");
        el.className =
            "chat-msg chat-msg-" + (msg.role === "user" ? "user" : "assistant");
        el.dataset.msgId = msg.id;

        if (msg.role === "user") {
            const bubble = document.createElement("div");
            bubble.className = "chat-msg-bubble";
            bubble.textContent = msg.content || "";
            el.appendChild(bubble);
            // User-message actions: Copy + Edit + Delete. Edit pulls
            // the message into the input box for re-send (the user
            // can revise their prompt and re-send to regenerate all
            // assistant responses below it).
            const actions = document.createElement("div");
            actions.className = "chat-msg-actions";
            actions.appendChild(makeAction("Copy", () => navigator.clipboard.writeText(msg.content || "")));
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
                h.textContent =
                    resp.slot_label ||
                    (model && model.name) ||
                    resp.model_id ||
                    "";
                col.appendChild(h);
            }
            const bubble = document.createElement("div");
            bubble.className = "chat-msg-bubble";
            const md = document.createElement("div");
            md.className = "chat-msg-md";
            md.innerHTML = renderMarkdown(resp.content || "");
            bubble.appendChild(md);
            if (resp.error) {
                const err = document.createElement("div");
                err.className = "chat-msg-error";
                err.textContent = "Error: " + resp.error;
                bubble.appendChild(err);
            }
            if (resp.cost_microdollars || resp.tokens_in || resp.tokens_out) {
                const meta = document.createElement("div");
                meta.className = "chat-msg-meta";
                const cents = (resp.cost_microdollars || 0) / 10_000;
                meta.textContent =
                    "$" +
                    (cents / 100).toFixed(4) +
                    "  ·  " +
                    (resp.tokens_in || 0) +
                    " in / " +
                    (resp.tokens_out || 0) +
                    " out" +
                    (responses.length === 1
                        ? "  ·  " + (resp.model_id || "")
                        : "");
                bubble.appendChild(meta);
            }
            const acts = document.createElement("div");
            acts.className = "chat-msg-actions chat-msg-col-actions";
            acts.appendChild(makeAction("Copy", () => navigator.clipboard.writeText(resp.content || "")));
            acts.appendChild(makeAction("Regenerate", () => regenerateResponse(chat, msg, respIdx)));
            col.appendChild(bubble);
            col.appendChild(acts);
            grid.appendChild(col);
        });
        el.appendChild(grid);

        // Whole-assistant-message actions
        const actions = document.createElement("div");
        actions.className = "chat-msg-actions chat-msg-msg-actions";
        actions.appendChild(makeAction("Delete", () => deleteMessage(chat, msg)));
        el.appendChild(actions);
        return el;
    }

    function makeAction(label, handler) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "chat-msg-action";
        b.textContent = label;
        b.addEventListener("click", handler);
        return b;
    }

    function deleteMessage(chat, msg) {
        chat.messages = chat.messages.filter((m) => m.id !== msg.id);
        chat.updated_at = isoNow();
        saveState();
        renderThread();
    }

    function editUserMessage(chat, msg) {
        const input = document.querySelector("[data-chat-input]");
        if (!input) return;
        input.value = msg.content || "";
        // Drop this user message and the assistant message immediately
        // after it (if any) — pressing Send again will regenerate.
        const idx = chat.messages.indexOf(msg);
        if (idx >= 0) {
            const next = chat.messages[idx + 1];
            const toRemove = new Set([msg.id]);
            if (next && next.role === "assistant") toRemove.add(next.id);
            chat.messages = chat.messages.filter((m) => !toRemove.has(m.id));
            chat.updated_at = isoNow();
            saveState();
            renderThread();
        }
        input.focus();
        autoResize(input);
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

    function renderModelPicker() {
        if (!pickerEl) return;
        const list = pickerEl.querySelector(".chat-model-picker-list");
        if (!list) return;
        list.innerHTML = "";
        const q = pickerQuery.toLowerCase();
        const filtered = MODELS.filter(
            (m) =>
                !q ||
                m.id.toLowerCase().includes(q) ||
                (m.name || "").toLowerCase().includes(q),
        ).slice(0, 100);
        for (const m of filtered) {
            const row = document.createElement("button");
            row.type = "button";
            row.className = "chat-model-row";
            row.innerHTML = `
                <div class="chat-model-row-name">${escapeHtml(m.name || m.id)}</div>
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
                    ${m.free ? '<span class="chat-tag-free">Free</span>' : ""}
                </div>
            `;
            row.addEventListener("click", () => {
                selectModel(m.id);
                closeModelPicker();
            });
            list.appendChild(row);
        }
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
        renderModelPicker();
        // ESC to close
        document.addEventListener("keydown", pickerKeyHandler);
    }

    function pickerKeyHandler(e) {
        if (e.key === "Escape") closeModelPicker();
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
        const userMsg = {
            id: newMsgId(),
            role: "user",
            content: text,
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
                messages.push({ role: "user", content: m.content });
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
        const abort = new AbortController();
        STREAMS.set(assistantMsg.id, abort);
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
                        saveState();
                        renderThread();
                        return;
                    }
                    try {
                        const ev = JSON.parse(payload);
                        const delta =
                            ev.choices && ev.choices[0] && ev.choices[0].delta;
                        if (delta && typeof delta.content === "string") {
                            respSlot.content += delta.content;
                            // Live re-render this message's bubble
                            patchAssistantBubble(assistantMsg, respSlot);
                        }
                        if (ev.usage) {
                            respSlot.tokens_in = ev.usage.prompt_tokens || 0;
                            respSlot.tokens_out =
                                ev.usage.completion_tokens || 0;
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
            saveState();
            renderThread();
        } finally {
            STREAMS.delete(assistantMsg.id);
        }
    }

    function patchAssistantBubble(msg, respSlot) {
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
            const thread = document.querySelector("[data-chat-thread]");
            if (thread) thread.scrollTop = thread.scrollHeight;
        }
    }

    // ── Input auto-resize ─────────────────────────────────────────────

    function autoResize(input) {
        if (!input) return;
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 200) + "px";
    }

    // ── Init ──────────────────────────────────────────────────────────

    function init() {
        bootstrapBrowserKey();
        ensureActiveChat();
        renderSidebar();
        renderModelsBar();
        renderSystemPrompt();
        renderThread();
        loadModels();

        const sendBtn = document.querySelector("[data-chat-send]");
        if (sendBtn) sendBtn.addEventListener("click", handleSendClick);

        const input = document.querySelector("[data-chat-input]");
        if (input) {
            input.addEventListener("input", () => autoResize(input));
            input.addEventListener("keydown", (e) => {
                if (
                    (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ||
                    (e.key === "Enter" && !e.shiftKey && window.innerWidth >= 780)
                ) {
                    e.preventDefault();
                    handleSendClick(e);
                }
            });
        }

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
                if (sidebar) {
                    const open = sidebar.dataset.open === "true";
                    sidebar.dataset.open = open ? "false" : "true";
                }
                return;
            }
            // Click outside any pill or dropdown closes the open dropdown.
            if (openDropdownSlotIdx >= 0 && !target.closest(".chat-model-pill-wrap")) {
                openDropdownSlotIdx = -1;
                renderModelsBar();
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
