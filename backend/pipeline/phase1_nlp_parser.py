import json
import os

PROMPT = """Convert this test instruction into a JSON list of actions.
Each action must have: "action" (tap/type/verify/scroll/back/wait), "target" (what element),
and "value" (only if typing text).

Instruction: "{text}"

Return ONLY valid JSON, nothing else. Example format:
[{{"action": "tap", "target": "login_button"}}]
"""


def _extract_list(raw: str):
    """Pull a JSON list out of the model text. Also accepts {"actions": [...]}."""
    start, end = raw.find("["), raw.rfind("]") + 1
    if start != -1 and end > start:
        return json.loads(raw[start:end])
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start != -1 and end > start:
        obj = json.loads(raw[start:end])
        for v in obj.values():
            if isinstance(v, list):
                return v
    raise ValueError(f"no JSON list in model output: {raw[:120]!r}")


def parse_instruction(user_text: str):
    provider = os.environ.get("VEROFLOW_LLM", "ollama").strip().lower()
    prompt = PROMPT.format(text=user_text)

    if provider in ("anthropic", "openai"):
        # Hosted model via llm_client (same config as the agent runner)
        from pipeline.llm_client import chat
        raw = chat(prompt)
    else:
        # Local mode: Ollama
        import ollama
        resp = ollama.chat(model="llama3.2",
                           messages=[{"role": "user", "content": prompt}])
        raw = resp["message"]["content"]

    try:
        return _extract_list(raw)
    except Exception as e:
        print(f"Could not parse AI response: {e}")
        return []