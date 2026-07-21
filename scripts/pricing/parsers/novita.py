"""Novita pricing parser for its server-rendered catalog and legacy tables."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

# Display name (as shown in the Model Name column) → OR-canonical id.
_DISPLAY_TO_OR_ID: dict[str, str] = {
    # Deepseek
    "Deepseek V4 Pro": "deepseek/deepseek-v4-pro",
    "Deepseek V4 Flash": "deepseek/deepseek-v4-flash",
    "Deepseek V3.2": "deepseek/deepseek-v3.2",
    "DeepSeek-OCR 2": "deepseek/deepseek-ocr-2",
    "Deepseek V3.2 Exp": "deepseek/deepseek-v3.2-exp",
    "Deepseek V3.1 Terminus": "deepseek/deepseek-v3.1-terminus",
    "DeepSeek V3.1": "deepseek/deepseek-v3.1",
    "DeepSeek V3 0324": "deepseek/deepseek-chat-v3-0324",
    "DeepSeek R1 0528": "deepseek/deepseek-r1-0528",
    "DeepSeek R1 Distill LLama 70B": "deepseek/deepseek-r1-distill-llama-70b",
    "DeepSeek V3 (Turbo)": "deepseek/deepseek-chat-v3-turbo",
    "DeepSeek R1 (Turbo)": "deepseek/deepseek-r1-turbo",
    # Qwen
    "Qwen3.7-Max": "qwen/qwen3.7-max",
    "Qwen3.6-27B": "qwen/qwen3.6-27b",
    "Qwen3.5-27B": "qwen/qwen3.5-27b",
    "Qwen3.5-122B-A10B": "qwen/qwen3.5-122b-a10b",
    "Qwen3.5-35B-A3B": "qwen/qwen3.5-35b-a3b",
    "Qwen3.5-397B-A17B": "qwen/qwen3.5-397b-a17b",
    "Qwen3 Coder Next": "qwen/qwen3-coder-next",
    "Qwen3 VL 235B A22B Thinking": "qwen/qwen3-vl-235b-a22b-thinking",
    "Qwen3.6-35B-A3B": "qwen/qwen3.6-35b-a3b",
    "Qwen3 Next 80B A3B Instruct": "qwen/qwen3-next-80b-a3b-instruct",
    "Qwen3 VL 235B A22B Instruct": "qwen/qwen3-vl-235b-a22b-instruct",
    "Qwen3 Coder 480B A35B Instruct": "qwen/qwen3-coder",
    "Qwen3 Coder 30b A3B Instruct": "qwen/qwen3-coder-30b-a3b-instruct",
    "Qwen3 235B A22b Thinking 2507": "qwen/qwen3-235b-a22b-thinking-2507",
    "Qwen3 235B A22B Instruct 2507": "qwen/qwen3-235b-a22b-2507",
    "Qwen 2.5 72B Instruct": "qwen/qwen-2.5-72b-instruct",
    "Qwen3 235B A22B": "qwen/qwen3-235b-a22b",
    "qwen/qwen3-vl-30b-a3b-instruct": "qwen/qwen3-vl-30b-a3b-instruct",
    "Qwen MT Plus": "qwen/qwen-mt-plus",
    # Baidu
    "CoBuddy": "baidu/cobuddy",
    "ERNIE 4.5 VL 424B A47B": "baidu/ernie-4.5-vl-424b-a47b",
    "ERNIE 4.5 21B A3B": "baidu/ernie-4.5-21b-a3b",
    # Zai-org / GLM
    "GLM 5.2": "z-ai/glm-5.2",
    "GLM-5.1": "z-ai/glm-5.1",
    "GLM-5": "z-ai/glm-5",
    "GLM-4.7-Flash": "z-ai/glm-4.7-flash",
    "GLM-4.7": "z-ai/glm-4.7",
    "AutoGLM-Phone-9B-Multilingual": "z-ai/autoglm-phone-9b-multilingual",
    "GLM 4.6V": "z-ai/glm-4.6v",
    "GLM 4.6": "z-ai/glm-4.6",
    "GLM 4.5V": "z-ai/glm-4.5v",
    "zai-org/glm-4.5-air": "z-ai/glm-4.5-air",
    # Sao10k
    "Sao10k L3 8B Lunaris": "sao10k/l3-lunaris-8b",
    "L3 8B Stheno V3.2": "sao10k/l3-stheno-8b",
    "L31 70B Euryale V2.2": "sao10k/l3.1-euryale-70b",
    # Hunyuan
    "Hy3": "tencent/hy3",
    # MoonshotAI
    "Kimi K3": "moonshotai/kimi-k3",
    "Kimi K2.7 Code": "moonshotai/kimi-k2.7-code",
    "Kimi K2.6": "moonshotai/kimi-k2.6",
    "Kimi K2.5": "moonshotai/kimi-k2.5",
    "Kimi K2 Thinking": "moonshotai/kimi-k2-thinking",
    "Kimi K2 0905": "moonshotai/kimi-k2-0905",
    "Kimi K2 Instruct": "moonshotai/kimi-k2",
    # MiniMax
    "MiniMax M2.7": "minimax/minimax-m2.7",
    "MiniMax M2.5-highspeed": "minimax/minimax-m2.5-highspeed",
    "MiniMax M2.5": "minimax/minimax-m2.5",
    "Minimax M2.1": "minimax/minimax-m2.1",
    "MiniMax-M2": "minimax/minimax-m2",
    "MiniMax M1": "minimax/minimax-m1",
    # StepFun
    "Step 3.7 Flash": "stepfun/step-3.7-flash",
    # Nvidia
    "Nemotron 3 Nano 30B A3B": "nvidia/nemotron-3-nano-30b-a3b",
    # Gemma
    "Gemma 4 26B A4B": "google/gemma-4-26b-a4b",
    "Gemma 4 31B": "google/gemma-4-31b",
    "Gemma 3 27B": "google/gemma-3-27b-it",
    # KwaiKAT
    "Kat Coder Pro": "kwaikat/kat-coder-pro",
    # OpenAI
    "OpenAI GPT OSS 120B": "openai/gpt-oss-120b",
    "OpenAI: GPT OSS 20B": "openai/gpt-oss-20b",
    # Llama
    "Llama 3.1 8B Instruct": "meta-llama/llama-3.1-8b-instruct",
    "Llama 3.3 70B Instruct": "meta-llama/llama-3.3-70b-instruct",
    "Llama 4 Maverick Instruct": "meta-llama/llama-4-maverick",
    "Llama 4 Scout Instruct": "meta-llama/llama-4-scout",
    # Mistral
    "Mistral Nemo": "mistralai/mistral-nemo",
    # Others
    "XiaomiMiMo/MiMo-V2.5": "xiaomi/mimo-v2.5",
    "XiaomiMiMo/MiMo-V2.5-Pro": "xiaomi/mimo-v2.5-pro",
    "Wizardlm 2 8x22B": "microsoft/wizardlm-2-8x22b",
    "Ring-2.6-1T": "inclusionai/ring-2.6-1t",
    "Ling-2.6-flash": "inclusionai/ling-2.6-flash",
    "Ling-2.6-1T": "inclusionai/ling-2.6-1t",
}


_LINK_PRICE_RE = re.compile(
    r"\| \[([\w./:_\-]+)\][^|]*"
    r"\|\s*[\d,]+\s*"
    r"\|\s*\$([\d.]+)\s*/Mt"
    r"(?:[^|]*?Cache Read \$([\d.]+)\s*/Mt)?"
    r"[^|]*"
    r"\|\s*\$([\d.]+)\s*/Mt"
    r"[^|]*"
    r"\|"
)

_FREE_TEXT_ROW_RE = re.compile(
    r"(?m)^\s*(?P<name>[^\t\n]+)\t[\d,]+\s*\n"
    r"\s*Free\s*\n\s*\n?\s*Free\s*\n",
)


# Match a model row. Because rows may span multiple lines (due to
# cache-read info), we use a somewhat loose pattern.
#
# Pattern: name, then TAB, then digits with commas (context),
# then everything up to /Mt (input price with optional cache-read),
# then output price /Mt.
_ROW_RE = re.compile(
    r"(?m)^(?P<name>[^\t\n][^\t\n]*?)\t"
    r"(?P<ctx>[\d,]+)\s*(?:\t|\n)"
    r"(?P<pricing>.*?)"
    r"(?=\n[^\t\n]*?\t[\d,]+\s*(?:\t|\n)|\nEmbeddings|\nImage\n|\Z)",
    re.DOTALL,
)

_INPUT_PRICE_RE = re.compile(r"\$([\d.]+)\s*/Mt")
_CACHE_PRICE_RE = re.compile(r"Cache Read\s*\$([\d.]+)\s*/Mt")
_FREE_RE = re.compile(r"\bFree\b")
_NEXT_PUSH_PREFIX = "self.__next_f.push("
_NEXT_MODELS_KEY = "initialFullLLMModels"
_NEXT_MODELS_MARKER = '"initialFullLLMModels":'
_PRICE_TEXT_RE = re.compile(r"\$+([\d.]+)\s*/\s*M(?:t|\s+tokens)", re.I)


def _usd_per_m_to_micro(raw: object) -> int | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return int((value * Decimal(1_000_000)).to_integral_value())


def _embedded_price(
    source: dict,
    *,
    info_field: str,
    numeric_field: str,
) -> int | None:
    infos = source.get("infos")
    if isinstance(infos, dict):
        match = _PRICE_TEXT_RE.search(str(infos.get(info_field) or ""))
        if match:
            return _usd_per_m_to_micro(match.group(1))

    # Novita's numeric pricePerM unit is 1/10,000 USD per million tokens.
    # Prefer the human-readable USD field above, but retain this fallback for
    # catalog rows whose display string is absent.
    pricing = source.get(numeric_field)
    if not isinstance(pricing, dict):
        return None
    raw = pricing.get("pricePerM")
    try:
        value = Decimal(str(raw)) * Decimal(100)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return int(value.to_integral_value())


def _canonical_embedded_id(source: dict) -> str | None:
    raw_id = source.get("id")
    display_name = source.get("displayName")
    if isinstance(display_name, str) and display_name in _DISPLAY_TO_OR_ID:
        return _DISPLAY_TO_OR_ID[display_name]
    if not isinstance(raw_id, str) or "/" not in raw_id:
        return None
    if raw_id.startswith("zai-org/"):
        return f"z-ai/{raw_id.removeprefix('zai-org/')}"
    return raw_id


def _next_catalog_rows(html: str) -> list[dict]:
    """Decode the model list embedded in Novita's server-rendered Next page."""

    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if _NEXT_MODELS_KEY not in text or not text.startswith(_NEXT_PUSH_PREFIX):
            continue
        try:
            payload = json.loads(text[len(_NEXT_PUSH_PREFIX) : -1])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], str):
            continue
        flight = payload[1]
        marker_index = flight.find(_NEXT_MODELS_MARKER)
        if marker_index < 0:
            continue
        start = marker_index + len(_NEXT_MODELS_MARKER)
        try:
            rows, _end = json.JSONDecoder().raw_decode(flight, start)
        except json.JSONDecodeError:
            continue
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _parse_embedded_catalog(html: str) -> dict:
    out: dict = {}
    for source in _next_catalog_rows(html):
        if source.get("type") != "Chat":
            continue
        status = source.get("status")
        if isinstance(status, int) and status <= 0:
            continue
        model_id = _canonical_embedded_id(source)
        if model_id is None:
            continue
        prompt = _embedded_price(
            source,
            info_field="inputPricing",
            numeric_field="input_pricing",
        )
        completion = _embedded_price(
            source,
            info_field="outputPricing",
            numeric_field="output_pricing",
        )
        if prompt is None or completion is None:
            continue
        row: dict = {
            "prompt_micro_per_m": prompt,
            "completion_micro_per_m": completion,
        }
        cached = _embedded_price(
            source,
            info_field="cacheReadPricing",
            numeric_field="cache_read_input_pricing",
        )
        # A zero cache field in Novita's payload means the model has no cache
        # price, not that cache reads are free.
        if cached is not None and cached > 0:
            row["prompt_cached_micro_per_m"] = cached
        out[model_id] = row
    return out


def _parse_dom_tables(html: str) -> dict:
    """Parse one model at a time from Novita's server-rendered table rows."""

    out: dict = {}
    soup = BeautifulSoup(html, "html.parser")
    for table_row in soup.find_all("tr"):
        cells = table_row.find_all(["th", "td"], recursive=False)
        if len(cells) < 4:
            continue
        name = cells[0].get_text(" ", strip=True)
        model_id = _DISPLAY_TO_OR_ID.get(name)
        if model_id is None:
            link = cells[0].find("a")
            link_text = link.get_text(" ", strip=True) if link else ""
            if "/" in link_text:
                model_id = link_text
        if model_id is None:
            continue

        input_text = cells[2].get_text(" ", strip=True)
        output_text = cells[3].get_text(" ", strip=True)
        input_match = _PRICE_TEXT_RE.search(input_text)
        output_match = _PRICE_TEXT_RE.search(output_text)
        if input_match and output_match:
            prompt = _usd_per_m_to_micro(input_match.group(1))
            completion = _usd_per_m_to_micro(output_match.group(1))
        elif _FREE_RE.search(input_text) and _FREE_RE.search(output_text):
            prompt = completion = 0
        else:
            continue
        if prompt is None or completion is None:
            continue
        row = {
            "prompt_micro_per_m": prompt,
            "completion_micro_per_m": completion,
        }
        cache_match = _CACHE_PRICE_RE.search(input_text)
        if cache_match:
            cached = _usd_per_m_to_micro(cache_match.group(1))
            if cached is not None:
                row["prompt_cached_micro_per_m"] = cached
        out[model_id] = row
    return out


def parse(html: str) -> dict:
    # The embedded catalog carries canonical IDs and row-local prices. Parsing
    # it first prevents a flattened page projection from associating one
    # model's input price with another model's output price.
    out: dict = _parse_embedded_catalog(html)
    for model_id, row in _parse_dom_tables(html).items():
        out.setdefault(model_id, row)

    # Historical captured renders use markdown links with canonical model IDs.
    for match in _LINK_PRICE_RE.finditer(html):
        model_id, input_usd, cached_usd, output_usd = match.groups()
        if model_id in out:
            continue
        try:
            row: dict = {
                "prompt_micro_per_m": int(round(float(input_usd) * 1_000_000)),
                "completion_micro_per_m": int(round(float(output_usd) * 1_000_000)),
            }
        except ValueError:
            continue
        if cached_usd:
            try:
                row["prompt_cached_micro_per_m"] = int(round(float(cached_usd) * 1_000_000))
            except ValueError:
                pass
        out[model_id] = row

    for match in _FREE_TEXT_ROW_RE.finditer(html):
        name = match.group("name").strip()
        model_id = _DISPLAY_TO_OR_ID.get(name)
        if model_id and model_id not in out:
            out[model_id] = {
                "prompt_micro_per_m": 0,
                "completion_micro_per_m": 0,
            }

    # Historical plain-text renders use display names in a tab-delimited table.
    # Never run this loose compatibility parser over a live HTML document.
    plain_text_matches = (
        () if re.search(r"<\s*(?:!doctype|html|body)\b", html, re.I) else _ROW_RE.finditer(html)
    )
    for match in plain_text_matches:
        name = match.group("name").strip()
        if not name or name not in _DISPLAY_TO_OR_ID:
            continue
        or_id = _DISPLAY_TO_OR_ID[name]
        if or_id in out:
            continue
        pricing = match.group("pricing")

        cache_match = _CACHE_PRICE_RE.search(pricing)
        cache_usd = None
        if cache_match:
            cache_usd = cache_match.group(1)
            # Remove the cache read segment so it doesn't confuse
            # the input/output extraction.
            pricing_stripped = _CACHE_PRICE_RE.sub("", pricing)
        else:
            pricing_stripped = pricing

        prices = _INPUT_PRICE_RE.findall(pricing_stripped)
        free_hits = _FREE_RE.findall(pricing_stripped)

        input_micro = None
        output_micro = None

        if len(prices) >= 2:
            try:
                input_micro = int(round(float(prices[0]) * 1_000_000))
                output_micro = int(round(float(prices[-1]) * 1_000_000))
            except ValueError:
                continue
        elif len(free_hits) >= 2:
            input_micro = 0
            output_micro = 0
        else:
            # skip rows without clear numeric input+output
            continue

        row: dict = {
            "prompt_micro_per_m": input_micro,
            "completion_micro_per_m": output_micro,
        }
        if cache_usd is not None:
            try:
                row["prompt_cached_micro_per_m"] = int(round(float(cache_usd) * 1_000_000))
            except ValueError:
                pass
        out[or_id] = row
    return out
