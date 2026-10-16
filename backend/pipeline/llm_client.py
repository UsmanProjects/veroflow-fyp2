import os
import io
import json
import time
import base64

SCREEN_PROMPT = """Look at this app screenshot. Answer in this format:

Screen Name: (short name for this screen, e.g. "Login Screen")
Elements: (list buttons, text fields, and labels you can see)"""

DEFAULT_MODELS = {"anthropic": "claude-haiku-4-5-20251001"}
_CALLS = {"n": 0}


class LLMError(RuntimeError):
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def _cfg():
    return {
        "provider": os.environ.get("VEROFLOW_LLM", "ollama").strip().lower(),
        "model": os.environ.get("VEROFLOW_MODEL", "").strip(),
        "base_url": os.environ.get("VEROFLOW_BASE_URL", "").strip(),
        "key": os.environ.get("VEROFLOW_API_KEY", "").strip(),
        "max_tokens": int(os.environ.get("VEROFLOW_MAX_TOKENS", "800")),
        "retries": max(1, int(os.environ.get("VEROFLOW_RETRIES", "3"))),
        "effort": os.environ.get("VEROFLOW_REASONING_EFFORT", "").strip(),
    }


def _image_b64(path, max_side=1280):
    """Downscale the screenshot (faster and cheaper) and return base64 PNG."""
    try:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        scale = max_side / max(img.size)
        if scale < 1:
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()


def _retry(fn, tries=3):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            if getattr(e, "retryable", True) is False or i == tries - 1:
                break
            time.sleep(min(2 ** (i + 1), 20))
    raise last


def _anthropic_call(prompt, image_path, cfg):
    import anthropic
    client = anthropic.Anthropic(api_key=cfg["key"] or None)
    content = []
    if image_path:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": _image_b64(image_path)}})
    content.append({"type": "text", "text": prompt})
    resp = client.messages.create(
        model=cfg["model"] or DEFAULT_MODELS["anthropic"],
        max_tokens=cfg["max_tokens"],
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _openai_call(prompt, image_path, cfg):
    import httpx
    content = [{"type": "text", "text": prompt}]
    if image_path:
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + _image_b64(image_path)}})
    payload = {"model": cfg["model"], "messages": [{"role": "user", "content": content}],
               "max_tokens": cfg["max_tokens"]}
    if cfg["effort"]:
        payload["reasoning_effort"] = cfg["effort"]
    r = httpx.post(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {cfg['key']}"},
        json=payload,
        timeout=120,
    )
    if r.status_code >= 400:
        retryable = r.status_code == 429 or r.status_code >= 500
        raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}", retryable=retryable)
    text = r.json()["choices"][0]["message"]["content"]
    if not text:
        raise LLMError("model returned empty content (raise VEROFLOW_MAX_TOKENS or lower reasoning effort)",
                       retryable=False)
    return text


def chat(prompt, image_path=None):
    """Hosted-model call. Raises for provider 'ollama' (local models use their own code)."""
    cfg = _cfg()
    started = time.time()
    if cfg["provider"] == "anthropic":
        out = _retry(lambda: _anthropic_call(prompt, image_path, cfg), cfg["retries"])
    elif cfg["provider"] == "openai":
        if not cfg["base_url"] or not cfg["model"]:
            raise LLMError("VEROFLOW_BASE_URL and VEROFLOW_MODEL must be set for provider 'openai'", retryable=False)
        out = _retry(lambda: _openai_call(prompt, image_path, cfg), cfg["retries"])
    else:
        raise LLMError("no hosted provider configured", retryable=False)
    _CALLS["n"] += 1
    print(f"[llm] {cfg['provider']}/{cfg['model'] or 'default'} request #{_CALLS['n']} "
          f"ok in {time.time() - started:.1f}s", flush=True)
    return out


def chat_json(prompt, image_path=None):
    """Returns a parsed JSON object. Local mode: llama3.2 via Ollama (text only)."""
    cfg = _cfg()
    if cfg["provider"] in ("anthropic", "openai"):
        text = chat(prompt, image_path)
        start, end = text.find("{"), text.rfind("}") + 1
        if start == -1 or end <= start:
            raise ValueError(f"model did not return JSON: {text[:120]!r}")
        return json.loads(text[start:end])
    import ollama
    resp = ollama.chat(
        model="llama3.2",
        messages=[{"role": "user", "content": prompt}],
        format="json",
        options={"temperature": 0},
        keep_alive="10m",
    )
    return json.loads(resp["message"]["content"])


def label_screen(screenshot_path):
    """Screen description. Hosted provider if configured, otherwise local Qwen2.5-VL."""
    if _cfg()["provider"] in ("anthropic", "openai"):
        return chat(SCREEN_PROMPT, screenshot_path).strip()
    from pipeline.phase3_screen_labeler import label_screen as local_label
    return local_label(screenshot_path)
