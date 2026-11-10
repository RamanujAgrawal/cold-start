"""A tiny client for any OpenAI-compatible chat API, standard library only.

It reads LLM_BASE_URL, LLM_API_KEY and LLM_MODEL from the environment, which is how we run your
code with our own key when we score it. Every call appends its token counts to usage.jsonl, so you
can see what a run costs before we do.

    from llm import chat
    reply = chat([{"role": "user", "content": "Hello"}])

You don't have to use this file. Any client is fine, as long as it reads those three variables and
sends nothing anywhere else.
"""
import json
import os
import time
import urllib.request
import sys


import json
import os
import time
import urllib.request
import sys
from pathlib import Path


def _load_env_if_needed():
    # If LLM_API_KEY is not in env, check for .env file
    if "LLM_API_KEY" not in os.environ:
        for p in [Path(".env"), Path(__file__).resolve().parent.parent / ".env"]:
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
                break


_KEY_INDEX = 0


def _get_api_keys():
    """Retrieve list of available API keys from environment or .env."""
    _load_env_if_needed()
    raw = os.environ.get("LLM_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys if keys else [""]


def chat(messages, model=None, temperature=0, max_tokens=None, response_format=None, timeout=120):
    global _KEY_INDEX
    _load_env_if_needed()
    base = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    keys = _get_api_keys()
    body = {"model": model or os.environ.get("LLM_MODEL", "qwen/qwen3.8-27b"), "messages": messages, "temperature": temperature}
    if max_tokens:
        body["max_tokens"] = max_tokens
    if response_format:
        body["response_format"] = response_format
    
    # Dynamic pacing delay based on active pool size
    delay = 0.2 if len(keys) >= 5 else (0.5 if len(keys) >= 3 else (1.5 if len(keys) > 1 else 4.0))
    time.sleep(delay)
    
    started = time.time()
    max_retries = max(8, len(keys) * 3)
    
    for attempt in range(max_retries):
        current_key = keys[_KEY_INDEX % len(keys)]
        _KEY_INDEX += 1
        
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + current_key,
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) cold-start/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                # If multiple keys are available, immediately failover to next key in pool
                if len(keys) > 1 and attempt < len(keys):
                    print(f"  Key rate limited. Failover to next key in pool...", file=sys.stderr)
                    time.sleep(0.3 if len(keys) >= 5 else 0.8)
                    continue

                # Read error body to find exact wait time from Groq
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    pass
                
                import re
                wait = None
                m_match = re.search(r"try again in (?:(\d+)m)?([\d\.]+)s", err_text)
                if m_match:
                    mins = float(m_match.group(1)) if m_match.group(1) else 0.0
                    secs = float(m_match.group(2))
                    wait = mins * 60.0 + secs + 1.0
                else:
                    ms_match = re.search(r"try again in ([\d\.]+)ms", err_text)
                    if ms_match:
                        wait = float(ms_match.group(1)) / 1000.0 + 1.0
                
                if wait is not None:
                    wait = min(wait, 60.0)
                else:
                    retry_after = e.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = min(float(retry_after) + 1.0, 60.0)
                        except ValueError:
                            wait = 10.0 + 3.0 * attempt
                    else:
                        wait = 10.0 + 3.0 * attempt
                
                print(f"  Rate limited (attempt {attempt+1}). Waiting {wait:.1f}s...", file=sys.stderr)
                time.sleep(wait)
            elif attempt < max_retries - 1:
                wait = 2 ** attempt * 2
                print(f"  LLM call failed (attempt {attempt+1}): {e}. Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt * 2
                print(f"  LLM call failed (attempt {attempt+1}): {e}. Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise

    usage = data.get("usage") or {}
    with open("usage.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "model": body["model"],
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "seconds": round(time.time() - started, 2),
        }) + "\n")
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    # Some reasoning models may put output in content but also have a reasoning field
    # If content is empty but reasoning exists, that's a sign max_tokens was too low
    if not content.strip() and msg.get("reasoning"):
        print(f"  Warning: LLM returned empty content with reasoning (increase max_tokens?)", file=sys.stderr)
    return content
