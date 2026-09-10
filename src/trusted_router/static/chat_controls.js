"use strict";

// Pure request/state helpers shared by the browser and local contract tests.
(function (root) {
    function preferences(raw) {
        const out = raw && typeof raw === "object" ? { ...raw } : {};
        const sort = out.sort || out.sort_by;
        delete out.sort_by;
        if (sort === "cost") out.sort = "price";
        else if (["price", "latency", "throughput"].includes(sort)) out.sort = sort;
        else delete out.sort;
        if (Array.isArray(out.only)) out.only = [...out.only];
        return out;
    }

    function mode(slot) {
        return ["off", "on"].includes(slot.reasoning_mode) ? slot.reasoning_mode : "default";
    }

    function pin(slot) {
        const only = slot.provider_preferences?.only;
        return Array.isArray(only) && only.length === 1 ? only[0] : "";
    }

    function eligible(slot, endpoints, reasoning = mode(slot)) {
        const prefs = preferences(slot.provider_preferences);
        return endpoints.filter((endpoint) =>
            (!prefs.only?.length || prefs.only.includes(endpoint.provider)) &&
            (!prefs.ignore?.includes(endpoint.provider)) &&
            (reasoning === "default" || endpoint.trustedrouter?.reasoning_modes?.includes(reasoning)),
        );
    }

    function overrides(slot, endpoints) {
        const provider = preferences(slot.provider_preferences);
        const reasoning = mode(slot);
        const selected = eligible(slot, endpoints);
        const out = {};
        if (pin(slot)) {
            if (!selected.length) throw new Error("The pinned provider cannot serve these settings. Choose another provider or use default reasoning.");
            provider.allow_fallbacks = false;
        }
        if (reasoning !== "default") {
            if (!selected.length) throw new Error("No provider supports this reasoning setting for this model.");
            provider.only = [...new Set(selected.map((endpoint) => endpoint.provider))];
            out.reasoning = { enabled: reasoning === "on" };
        }
        const seed = slot.params?.seed;
        if (seed !== undefined && seed !== null && seed !== "") {
            if (!Number.isInteger(seed) || seed < 0 || seed > 2147483647) {
                throw new Error("Seed must be a whole number between 0 and 2147483647.");
            }
            const seeded = selected.filter((endpoint) => endpoint.supported_parameters?.includes("seed"));
            if (!seeded.length) throw new Error("No selected provider supports a seed for this model.");
            provider.only = [...new Set(seeded.map((endpoint) => endpoint.provider))];
            out.seed = seed;
        }
        if (Object.keys(provider).length) out.provider = provider;
        return out;
    }

    function editResponse(response, text, now) {
        if (typeof text !== "string" || !text.trim()) throw new Error("The response cannot be empty.");
        if (text === response.content) return false;
        response.content = text;
        response.edited_at = now;
        // Generated reasoning/tools describe the old answer, not this edit.
        // Keep every billed cost/token field and serving provenance untouched.
        delete response.reasoning;
        delete response.tool_calls;
        return true;
    }

    function linkLegacyResponses(chat) {
        for (const message of chat.messages || []) {
            const responses = Array.isArray(message.responses) ? message.responses : [];
            const used = new Set(responses.map((response) => response.slot_id).filter(Boolean));
            for (const response of responses) {
                if (response.slot_id) continue;
                // Old chats identified columns only by model and label. Match
                // repeated identical slots in display order, without collapsing
                // both histories into the first column.
                const slot = chat.models.find((candidate) => candidate.slot_id &&
                    !used.has(candidate.slot_id) && candidate.model_id === response.model_id &&
                    (candidate.label || "") === (response.slot_label || ""));
                if (slot) { response.slot_id = slot.slot_id; used.add(slot.slot_id); }
            }
        }
    }

    const controls = { preferences, mode, pin, eligible, overrides, editResponse, linkLegacyResponses };
    if (typeof module !== "undefined" && module.exports) module.exports = controls;
    else root.TrustedRouterChatControls = controls;
})(typeof window === "undefined" ? globalThis : window);
