/**
 * SSE response builders for mocked /v1/chat/completions calls.
 *
 * Mirrors the OpenAI delta protocol the chat client expects:
 *   data: {"id":"…","choices":[{"delta":{"content":"hello"}}]}
 *   data: {"id":"…","choices":[{"delta":{"content":" world"}}]}
 *   data: {"id":"…","choices":[{"finish_reason":"stop"}], "usage":{…}}
 *   data: [DONE]
 *
 * Tests build streams from a few simple primitives (chunked content,
 * reasoning content, tool_calls) instead of hand-rolling the wire
 * format each time.
 */

export interface SsePart {
    content?: string;
    reasoning?: string;
    tool_calls?: Array<{ id: string; type: "function"; function: { name: string; arguments: string } }>;
}

export interface SseStreamOptions {
    parts: SsePart[];
    promptTokens?: number;
    completionTokens?: number;
    delayBetweenParts?: number; // not used inline; the route handler chunks naturally
}

/** Build the canonical SSE body for a mocked completion. */
export function buildSseBody(opts: SseStreamOptions): string {
    const id = "chatcmpl-test-" + Math.random().toString(36).slice(2, 8);
    const lines: string[] = [];
    for (const part of opts.parts) {
        const delta: Record<string, unknown> = {};
        if (part.content !== undefined) delta.content = part.content;
        if (part.reasoning !== undefined) delta.reasoning = part.reasoning;
        if (part.tool_calls !== undefined) delta.tool_calls = part.tool_calls;
        const payload = {
            id,
            object: "chat.completion.chunk",
            choices: [{ index: 0, delta }],
        };
        lines.push("data: " + JSON.stringify(payload));
        lines.push("");
    }
    // Final chunk with finish_reason + usage so the client picks up
    // tokens/sec and cost.
    const finalPayload = {
        id,
        object: "chat.completion.chunk",
        choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
        usage: {
            prompt_tokens: opts.promptTokens ?? 12,
            completion_tokens:
                opts.completionTokens ??
                opts.parts.reduce(
                    (n, p) => n + (p.content?.split(" ").length ?? 0),
                    0,
                ),
            total_tokens:
                (opts.promptTokens ?? 12) +
                (opts.completionTokens ??
                    opts.parts.reduce(
                        (n, p) => n + (p.content?.split(" ").length ?? 0),
                        0,
                    )),
        },
    };
    lines.push("data: " + JSON.stringify(finalPayload));
    lines.push("");
    lines.push("data: [DONE]");
    lines.push("");
    return lines.join("\n");
}

/** A simple "hello world" SSE response. */
export function helloSse(): string {
    return buildSseBody({
        parts: [
            { content: "Hello" },
            { content: " " },
            { content: "world" },
            { content: "!" },
        ],
        promptTokens: 8,
        completionTokens: 4,
    });
}

/** SSE with a reasoning section before the answer. */
export function reasoningSse(): string {
    return buildSseBody({
        parts: [
            { reasoning: "Let me think about this carefully." },
            { reasoning: " The question is straightforward — answer briefly." },
            { content: "The capital of France is Paris." },
        ],
        promptTokens: 12,
        completionTokens: 8,
    });
}

/** SSE with a tool_calls chunk. */
export function toolCallSse(): string {
    return buildSseBody({
        parts: [
            {
                tool_calls: [
                    {
                        id: "call_test_1",
                        type: "function",
                        function: {
                            name: "get_weather",
                            arguments: '{"city":"Paris"}',
                        },
                    },
                ],
            },
            { content: "I'll look up the weather for you." },
        ],
        promptTokens: 15,
        completionTokens: 7,
    });
}

/** SSE with a markdown response including code + heading + list. */
export function markdownSse(): string {
    return buildSseBody({
        parts: [
            { content: "# Result\n\nHere's the snippet:\n\n```python\n" },
            { content: "def add(a, b):\n    return a + b\n```\n\n- works\n- tested\n" },
        ],
        promptTokens: 20,
        completionTokens: 30,
    });
}

/** A simple catalog response shape — minimal subset to render the picker. */
export function modelsCatalog(): unknown {
    return {
        data: [
            mkModel("anthropic/claude-opus-4.7", "Claude Opus 4.7", {
                input: 0.000015,
                output: 0.000075,
                context_length: 200_000,
                capabilities: ["vision", "tools"],
            }),
            mkModel("anthropic/claude-sonnet-4.6", "Claude Sonnet 4.6", {
                input: 0.000003,
                output: 0.000015,
                context_length: 200_000,
                capabilities: ["vision", "tools"],
            }),
            mkModel("openai/gpt-5.5", "GPT-5.5", {
                input: 0.0000025,
                output: 0.00001,
                context_length: 128_000,
                capabilities: ["vision", "tools"],
            }),
            mkModel("openai/gpt-5.4-nano", "GPT-5.4 Nano", {
                input: 0.0,
                output: 0.0,
                context_length: 32_000,
                capabilities: [],
            }),
            mkModel("google/gemini-2.5-flash", "Gemini 2.5 Flash", {
                input: 0.0000005,
                output: 0.000003,
                context_length: 1_000_000,
                capabilities: ["vision"],
            }),
            mkModel("mistralai/mistral-large", "Mistral Large", {
                input: 0.000003,
                output: 0.000009,
                context_length: 128_000,
                capabilities: ["tools"],
            }),
        ],
    };
}

function mkModel(
    id: string,
    name: string,
    extras: {
        input: number;
        output: number;
        context_length: number;
        capabilities: string[];
    },
): unknown {
    return {
        id,
        name,
        description: name,
        context_length: extras.context_length,
        pricing: {
            prompt: String(extras.input),
            completion: String(extras.output),
        },
        trustedrouter: {
            capabilities: extras.capabilities,
            uptime_pct: 99.95,
        },
    };
}
