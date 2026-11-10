"""A robust client for any OpenAI-compatible chat API, standard library only.

Features:
- Proactive per-key rate limiter (KeyPool) with 60-second sliding windows and RPM budget tracking.
- Preemptive cooldowns based on Groq response headers (x-ratelimit-remaining-*).
- Sidelining on 429: penalizes only the affected key and immediately rotates to an available key.
- Thread-safe key acquisition and usage tracking for concurrent workers.
"""
import collections
import json
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path


def _load_env_if_needed():
    if "LLM_API_KEY" not in os.environ:
        for p in [Path(".env"), Path(__file__).resolve().parent.parent / ".env"]:
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
                break


def _get_api_keys():
    """Retrieve list of available API keys from environment or .env."""
    _load_env_if_needed()
    raw = os.environ.get("LLM_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys if keys else [""]


def _parse_time_str(val):
    """Parse time representations like '12.5s', '1m30s', '450ms' into float seconds."""
    if not val:
        return None
    val = str(val).strip()
    try:
        return float(val)
    except ValueError:
        pass
    m = re.search(r"(?:(\d+)m)?([\d\.]+)s", val)
    if m:
        mins = float(m.group(1)) if m.group(1) else 0.0
        secs = float(m.group(2))
        return mins * 60.0 + secs
    ms = re.search(r"([\d\.]+)ms", val)
    if ms:
        return float(ms.group(1)) / 1000.0
    return None


class KeyPool:
    """Proactive per-key rate limiter with sliding window, cooldowns, and token awareness."""

    def __init__(self, keys, rpm=20, safety=0.85):
        self.keys = list(keys)
        self.limit = max(1, int(rpm * safety))  # e.g., 20 * 0.85 = 17 req per 60s
        self.windows = {k: collections.deque() for k in self.keys}
        self.cooldown = {k: 0.0 for k in self.keys}
        self.lock = threading.Lock()

    def update_keys(self, keys):
        with self.lock:
            for k in keys:
                if k not in self.windows:
                    self.windows[k] = collections.deque()
                    self.cooldown[k] = 0.0
            self.keys = list(keys)

    def acquire(self):
        """Block until a key has capacity, reserve slot, and return it."""
        while True:
            now = time.time()
            with self.lock:
                best_wait = None
                for k in self.keys:
                    w = self.windows[k]
                    # Purge timestamps older than 60s
                    while w and now - w[0] >= 60.0:
                        w.popleft()

                    if now < self.cooldown[k]:
                        wait = self.cooldown[k] - now
                    elif len(w) < self.limit:
                        w.append(now)  # Reserve the slot
                        return k
                    else:
                        wait = 60.0 - (now - w[0])

                    if best_wait is None or wait < best_wait:
                        best_wait = wait

            # If all keys are at capacity or cooling down, sleep until the earliest opens up
            sleep_time = max(0.05, min(best_wait if best_wait is not None else 0.5, 5.0))
            time.sleep(sleep_time)

    def penalize(self, key, seconds):
        """Take a key out of rotation for the given duration."""
        with self.lock:
            now = time.time()
            self.cooldown[key] = max(self.cooldown.get(key, 0.0), now + seconds)

    def update_from_headers(self, key, headers):
        """Proactively inspect Groq rate-limit headers to prevent 429s."""
        if not headers:
            return
        
        rem_req = headers.get("x-ratelimit-remaining-requests")
        reset_req = headers.get("x-ratelimit-reset-requests")
        rem_tok = headers.get("x-ratelimit-remaining-tokens")
        reset_tok = headers.get("x-ratelimit-reset-tokens")

        try:
            if rem_req is not None and int(rem_req) <= 1:
                secs = _parse_time_str(reset_req) or 5.0
                self.penalize(key, secs)
            elif rem_tok is not None and int(rem_tok) < 1500:
                secs = _parse_time_str(reset_tok) or 10.0
                self.penalize(key, secs)
        except Exception:
            pass


_POOL = None
_POOL_LOCK = threading.Lock()
_USAGE_LOCK = threading.Lock()


def _get_pool():
    global _POOL
    with _POOL_LOCK:
        keys = _get_api_keys()
        if _POOL is None:
            _POOL = KeyPool(keys, rpm=20, safety=0.85)
        else:
            _POOL.update_keys(keys)
        return _POOL


def chat(messages, model=None, temperature=0, max_tokens=None, response_format=None, timeout=120):
    """Send a chat completion request with proactive rate-limiting and failover."""
    _load_env_if_needed()
    base = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    body = {
        "model": model or os.environ.get("LLM_MODEL", "qwen/qwen3.8-27b"),
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    if response_format:
        body["response_format"] = response_format

    pool = _get_pool()
    max_retries = max(6, len(pool.keys) * 2)
    started = time.time()
    data = None

    for attempt in range(max_retries):
        key = pool.acquire()

        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) cold-start/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                headers = dict(resp.headers)
                data = json.loads(resp.read().decode("utf-8"))
                pool.update_from_headers(key, headers)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    pass

                wait = None
                m_match = re.search(r"try again in (?:(\d+)m)?([\d\.]+)s", err_text)
                if m_match:
                    mins = float(m_match.group(1)) if m_match.group(1) else 0.0
                    secs = float(m_match.group(2))
                    wait = mins * 60.0 + secs + 0.5
                else:
                    ms_match = re.search(r"try again in ([\d\.]+)ms", err_text)
                    if ms_match:
                        wait = float(ms_match.group(1)) / 1000.0 + 0.5

                if wait is None:
                    ra = e.headers.get("Retry-After")
                    if ra:
                        wait = _parse_time_str(ra) or 15.0
                    else:
                        wait = _parse_time_str(e.headers.get("x-ratelimit-reset-tokens")) or 15.0

                wait = min(max(wait, 2.0), 60.0)
                # Penalize only this specific key — other keys remain active immediately
                pool.penalize(key, wait)
                print(f"  Key rate-limited. Sidelined for {wait:.1f}s. Switching key...", file=sys.stderr)
                if attempt == max_retries - 1:
                    raise
                continue
            elif attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"  LLM call failed (attempt {attempt+1}): {e}. Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"  LLM call failed (attempt {attempt+1}): {e}. Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise

    if data is None:
        raise RuntimeError("No response data received from LLM")

    usage = data.get("usage") or {}
    with _USAGE_LOCK:
        try:
            with open("usage.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "model": body["model"],
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "seconds": round(time.time() - started, 2),
                }) + "\n")
        except Exception:
            pass

    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    if not content.strip() and msg.get("reasoning"):
        print(f"  Warning: LLM returned empty content with reasoning (increase max_tokens?)", file=sys.stderr)
    return content
