"""チケットの中で、お客様の質問にAIが答える。

無料枠のあるAIを Gemini → Groq → OpenRouter の順に試し、上限などで失敗したら次に切り替える。
使うのは、環境変数にキーが入っているものだけ。

環境変数
  GEMINI_API_KEY / GROQ_API_KEY / OPENROUTER_API_KEY : 各サービスのキー（ないものは使わない）
  GEMINI_MODEL / GROQ_MODEL / OPENROUTER_MODEL      : 使うモデル（サービス側で名前が変わったときに差し替える）
"""

import logging
import os

import aiohttp

logger = logging.getLogger("SupportBot.AI")

TIMEOUT = aiohttp.ClientTimeout(total=40)
MAX_OUTPUT_TOKENS = 700


def _providers():
    providers = []
    if os.getenv("GEMINI_API_KEY"):
        providers.append(("Gemini", _ask_gemini))
    if os.getenv("GROQ_API_KEY"):
        providers.append(("Groq", _ask_groq))
    if os.getenv("OPENROUTER_API_KEY"):
        providers.append(("OpenRouter", _ask_openrouter))
    return providers


def ai_enabled():
    return bool(_providers())


async def _ask_gemini(session, system, history):
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [
            {"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]}
            for role, text in history
        ],
        "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS, "temperature": 0.4},
    }
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}
    async with session.post(url, json=payload, headers=headers) as resp:
        data = await resp.json(content_type=None)
        if resp.status != 200:
            raise RuntimeError(f"Gemini {resp.status}: {str(data)[:200]}")
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


async def _ask_openai_style(session, url, key, model, system, history, extra_headers=None):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}]
                    + [{"role": role, "content": text} for role, text in history],
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.4,
    }
    headers = {"Authorization": f"Bearer {key}", **(extra_headers or {})}
    async with session.post(url, json=payload, headers=headers) as resp:
        data = await resp.json(content_type=None)
        if resp.status != 200:
            raise RuntimeError(f"{resp.status}: {str(data)[:200]}")
    return data["choices"][0]["message"]["content"]


async def _ask_groq(session, system, history):
    return await _ask_openai_style(
        session, "https://api.groq.com/openai/v1/chat/completions",
        os.environ["GROQ_API_KEY"], os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        system, history,
    )


async def _ask_openrouter(session, system, history):
    return await _ask_openai_style(
        session, "https://openrouter.ai/api/v1/chat/completions",
        os.environ["OPENROUTER_API_KEY"], os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
        system, history, {"X-Title": "Support Bot"},
    )


async def ask_ai(system, history):
    """history は [("user"|"assistant", 本文), ...]。答えられなければ None。"""
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        for name, ask in _providers():
            try:
                answer = (await ask(session, system, history) or "").strip()
                if answer:
                    return answer
            except Exception as error:  # 上限・通信エラーなどは次のAIへ
                logger.warning("AI(%s)で答えられませんでした: %s", name, str(error)[:300])
    return None
