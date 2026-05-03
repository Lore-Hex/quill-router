from __future__ import annotations

import os
import sys


def main() -> int:
    try:
        from openai import OpenAI
    except ImportError:
        print("Install with: uv run --with openai python scripts/smoke_openai_sdk.py", file=sys.stderr)
        return 2

    api_key = os.environ.get("TR_SMOKE_API_KEY")
    if not api_key:
        bearer_file = os.path.expanduser("~/.quill-gcp-q-001-bearer.txt")
        try:
            with open(bearer_file, encoding="utf-8") as handle:
                api_key = handle.read().strip()
        except FileNotFoundError:
            print("Set TR_SMOKE_API_KEY or create ~/.quill-gcp-q-001-bearer.txt", file=sys.stderr)
            return 2

    base_url = os.environ.get("TR_SMOKE_BASE_URL", "https://api.quillrouter.com/v1")
    model = os.environ.get("TR_SMOKE_MODEL", "claude-opus-4-7")
    client = OpenAI(api_key=api_key, base_url=base_url)
    stream = client.chat.completions.create(
        model=model,
        stream=True,
        messages=[{"role": "user", "content": "reply exactly PONG"}],
    )
    text = ""
    for event in stream:
        if event.choices and event.choices[0].delta.content:
            text += event.choices[0].delta.content
    print(text)
    if text.strip() != "PONG":
        print(f"unexpected response: {text!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
