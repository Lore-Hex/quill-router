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
            title.textContent = chat.title;
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
            '<h2>Try any model — zero tokens until you sign in.</h2>' +
            '<p>Pick a model above, type a prompt, hit Send. Compare up to 4 models side-by-side.</p>' +
            '<div class="chat-suggest-grid">' + grid + "</div>";
        empty.addEventListener("click", (e) => {
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
    }

    // Per-code-block copy button. After marked + DOMPurify rendering,
    // each <pre> gets a hover-revealed Copy button overlay. The button
    // copies the *raw* code (not the HTML) so the user gets clean text.
    function injectCodeCopyButtons(root) {
        const pres = root.querySelectorAll(".chat-msg-md pre");
        pres.forEach((pre) => {
            if (pre.querySelector(".chat-code-copy")) return; // already injected
            const code = pre.querySelector("code") || pre;
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
            // Make sure pre is positioned for the absolute child
            pre.style.position = pre.style.position || "relative";
            pre.appendChild(btn);
        });
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
        }).slice(0, 100);
        for (const m of filtered) {
            const row = document.createElement("button");
            row.type = "button";
            row.className = "chat-model-row";
            const provider = providerFromModelId(m.id);
            const avatarLetter = provider ? provider[0].toUpperCase() : "?";
            const avatarStyle = providerColor(provider);
            row.innerHTML = `
                <div class="chat-model-row-main">
                    <span class="chat-model-row-avatar" style="${avatarStyle}">${escapeHtml(avatarLetter)}</span>
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
                </div>
            `;
            row.addEventListener("click", () => {
                selectModel(m.id);
                closeModelPicker();
            });
            list.appendChild(row);
        }
    }

    // Pull "anthropic" out of "anthropic/claude-opus-4.7", "openai" out
    // of "openai/gpt-5.4-nano", etc. Falls back to the whole id when
    // there's no "/" (some catalog entries use unqualified ids).
    function providerFromModelId(id) {
        if (!id || typeof id !== "string") return "";
        const slash = id.indexOf("/");
        return slash > 0 ? id.slice(0, slash) : id;
    }

    // Deterministic per-provider tint so each row's avatar reads as
    // "from anthropic" vs "from openai" at a glance — matches the
    // OpenRouter visual cue of provider-colored circles without
    // requiring actual logo images. Hash the provider name to one
    // of N OR-style accent hues.
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
                            respSlot.content += delta.content;
                            // Live re-render this message's bubble
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
            STREAMS.delete(assistantMsg.id);
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

    // ── Keyboard shortcuts ────────────────────────────────────────────

    function handleGlobalShortcut(e) {
        // Skip if inside an input/textarea (except specific combos)
        const targetTag =
            e.target && e.target.tagName ? e.target.tagName.toLowerCase() : "";
        const inField = targetTag === "input" || targetTag === "textarea";
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

    function showShortcutsHelp() {
        const html =
            '<div class="chat-help-overlay" data-close>' +
            '<div class="chat-help-panel">' +
            "<h3>Keyboard shortcuts</h3>" +
            "<table>" +
            "<tr><td><kbd>⌘/Ctrl + Enter</kbd></td><td>Send message</td></tr>" +
            "<tr><td><kbd>⌘/Ctrl + N</kbd></td><td>New chat</td></tr>" +
            "<tr><td><kbd>⌘/Ctrl + E</kbd></td><td>Export to JSON</td></tr>" +
            "<tr><td><kbd>/</kbd></td><td>Focus input</td></tr>" +
            "<tr><td><kbd>K</kbd></td><td>Add model</td></tr>" +
            "<tr><td><kbd>Esc</kbd></td><td>Close menu</td></tr>" +
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
                if (t) t.scrollTop = t.scrollHeight;
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
