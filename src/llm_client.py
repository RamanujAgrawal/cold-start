"""Standard-library HTTP client for OpenAI-compatible chat completion APIs (Groq).

Why standard library?
The challenge evaluation rules require zero external dependencies (no requests,
aiohttp, or httpx). We implement this using urllib.request and Python threading.

Rate-Limit & Multi-Key Strategy:
Free-tier providers (like Groq) enforce strict rate limits: ~7,000 TPM and 30 RPM
per API key. For 4,500+ messages, naive single-key calls freeze for 60s repeatedly.
To solve this cleanly:
1. Multi-key round-robin pooling across available keys in .env.
2. Token bucket tracking with dynamic server header synchronization (x-ratelimit-remaining-tokens).
3. Dynamic retry delay extraction directly from 429 response bodies (waiting exact milliseconds/seconds
   instead of hardcoded sleep intervals).
4. Strictly in-memory (zero disk caching per challenge rules).
"""
import collections
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def _load_env_if_needed():
    """Load configuration from .env if LLM_API_KEY is not already present in os.environ."""
    if "LLM_API_KEY" not in os.environ:
        for p in [Path(".env"), Path(__file__).resolve().parent.parent / ".env"]:
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
                break


def _get_api_keys(pool_name="default"):
    """Retrieve list of available API keys from environment or .env.

    Supports:
    - pool_name in ('default', 'org_a', 'a'): returns LLM_API_KEY keys (Org A)
    - pool_name in ('stage5', 'org_b', 'b'): returns LLM_STAGE5_KEYS keys (Org B)
    """
    _load_env_if_needed()
    p = str(pool_name).lower() if pool_name else "default"
    if "stage5" in p or "org_b" in p or p == "b":
        raw = os.environ.get("LLM_STAGE5_KEYS", "")
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if keys:
            return keys
    raw = os.environ.get("LLM_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys if keys else [""]


def _parse_time_str(val):
    """Parse time representations like '12.5s', '1m30s', '450ms' into float seconds."""
    if val is None:
        return None
    val = str(val).strip()
    try:
        return float(val)
    except ValueError:
        pass
    # Check milliseconds first: e.g. "450ms"
    ms = re.search(r"([\d\.]+)ms", val)
    if ms:
        return float(ms.group(1)) / 1000.0
    # Check minutes and seconds: e.g. "1m30.5s" or "12.5s"
    m = re.search(r"(?:(\d+(?:\.\d+)?)m)?([\d\.]+)s", val)
    if m:
        mins = float(m.group(1)) if m.group(1) else 0.0
        secs = float(m.group(2))
        return mins * 60.0 + secs
    return None


def _parse_message_delay(text: str) -> float | None:
    """Extract wait duration from Groq rate limit error message strings."""
    if not text:
        return None
    # Check ms first: e.g. "try again in 450ms"
    ms_match = re.search(r"try again in ([\d\.]+)ms", text, re.IGNORECASE)
    if ms_match:
        return float(ms_match.group(1)) / 1000.0
    # Then minutes and seconds: e.g. "try again in 1m20.5s" or "try again in 2.5s"
    m_match = re.search(r"try again in (?:(\d+)m)?([\d\.]+)s", text, re.IGNORECASE)
    if m_match:
        mins = float(m_match.group(1)) if m_match.group(1) else 0.0
        secs = float(m_match.group(2))
        return mins * 60.0 + secs
    return None


def _extract_retry_delay(err_text: str, headers: dict) -> float:
    """Extract precise dynamic retry delay in seconds from error body and headers."""
    # 1. Try parsing JSON error body
    if err_text:
        try:
            err_json = json.loads(err_text)
            if isinstance(err_json, dict) and "error" in err_json:
                err_obj = err_json["error"]
                if isinstance(err_obj, dict):
                    # Check explicit retry_after field
                    if "retry_after" in err_obj:
                        delay = _parse_time_str(err_obj["retry_after"])
                        if delay is not None:
                            return delay
                    # Check message inside JSON
                    msg = err_obj.get("message", "")
                    if msg:
                        delay = _parse_message_delay(msg)
                        if delay is not None:
                            return delay
        except Exception:
            pass
        # Direct regex on raw body text
        delay = _parse_message_delay(err_text)
        if delay is not None:
            return delay

    # 2. Check HTTP headers
    if headers:
        hdr = {str(k).lower(): str(v) for k, v in headers.items()}
        ra = hdr.get("retry-after")
        if ra:
            delay = _parse_time_str(ra)
            if delay is not None:
                return delay

        reset_tokens = hdr.get("x-ratelimit-reset-tokens")
        if reset_tokens:
            delay = _parse_time_str(reset_tokens)
            if delay is not None:
                return delay

        reset_requests = hdr.get("x-ratelimit-reset-requests")
        if reset_requests:
            delay = _parse_time_str(reset_requests)
            if delay is not None:
                return delay

    # Default conservative fallback if no timing specified
    return 2.0


class OrganizationTokenBucket:
    """Proactive token-bucket rate limiter matching Groq's 8,000 TPM server-side bucket."""

    def __init__(self, name: str, keys: list[str], max_tpm: float = 7200.0, refill_per_sec: float = 120.0):
        self.name = name
        self.keys = [k.strip() for k in keys if k and k.strip()]
        self.key_idx = 0
        self.max_tokens = float(max_tpm)
        self.available_tokens = float(max_tpm)
        self.refill_rate = float(refill_per_sec)  # continuous refill rate in tokens/sec
        self.last_update = time.time()
        self.cooldown_until = 0.0
        self.lock = threading.Lock()

    def update_keys(self, keys: list[str]):
        """Update the pool's API keys."""
        with self.lock:
            self.keys = [k.strip() for k in keys if k and k.strip()]
            self.key_idx = 0

    def _refill_locked(self, now: float):
        """Internal helper to calculate continuous token refills up to max_tokens."""
        elapsed = max(0.0, now - self.last_update)
        self.available_tokens = min(self.max_tokens, self.available_tokens + elapsed * self.refill_rate)
        self.last_update = now

    def has_immediate_capacity_locked(self, now: float, estimated_tokens: float) -> bool:
        """Check if bucket currently has immediate capacity without waiting."""
        if not self.keys:
            return False
        if now < self.cooldown_until:
            return False
        self._refill_locked(now)
        needed = min(float(estimated_tokens), self.max_tokens)
        return self.available_tokens >= needed

    def wait_time_locked(self, now: float, estimated_tokens: float) -> float:
        """Compute the shortest wait time in seconds until this bucket has sufficient tokens."""
        if not self.keys:
            return float("inf")
        self._refill_locked(now)
        cooldown_wait = max(0.0, self.cooldown_until - now)
        needed = min(float(estimated_tokens), self.max_tokens) - self.available_tokens
        refill_wait = max(0.0, needed / self.refill_rate) if needed > 0 else 0.0
        return max(cooldown_wait, refill_wait)

    def reserve_locked(self, now: float, estimated_tokens: float) -> str:
        """Reserve estimated tokens from bucket and return next round-robin key."""
        self._refill_locked(now)
        reservation = min(float(estimated_tokens), self.max_tokens)
        self.available_tokens -= reservation

        # Round-robin key selection to space out RPM
        key = self.keys[self.key_idx % len(self.keys)]
        self.key_idx = (self.key_idx + 1) % len(self.keys)
        return key

    def set_cooldown(self, seconds: float):
        """Place organization bucket into cooldown (e.g. on 429 or TPD)."""
        with self.lock:
            now = time.time()
            self.cooldown_until = max(self.cooldown_until, now + seconds)
            self.available_tokens = 0.0
            self.last_update = now

    def update_from_headers(self, headers: dict | None):
        """Synchronize available tokens with Groq's authoritative server-side headers."""
        if not headers:
            return
        hdr = {str(k).lower(): str(v) for k, v in headers.items()}
        rem = hdr.get("x-ratelimit-remaining-tokens")
        if rem is not None:
            try:
                rem_val = float(str(rem).replace(",", "").strip())
                with self.lock:
                    self.available_tokens = min(rem_val, self.max_tokens)
                    self.last_update = time.time()
            except (ValueError, TypeError):
                pass

    # Backward-compatibility adapter methods
    def penalize(self, key: str, seconds: float):
        self.set_cooldown(seconds)

    def penalize_all(self, seconds: float):
        self.set_cooldown(seconds)

    def is_usable(self) -> bool:
        with self.lock:
            return len(self.keys) > 0 and time.time() >= self.cooldown_until

    def acquire(self, estimated_tokens: float = 600.0) -> str:
        now = time.time()
        with self.lock:
            wait = self.wait_time_locked(now, estimated_tokens)
            key = self.reserve_locked(now, estimated_tokens)
        if wait > 0:
            time.sleep(wait)
        return key


# Backward-compatible alias
KeyPool = OrganizationTokenBucket


class DualOrgDispatcher:
    """Manages cooperative minimum delay scheduling across Org A and Org B."""

    def __init__(self, max_tpm: float = 7200.0, refill_per_sec: float = 120.0):
        _load_env_if_needed()
        keys_a = _get_api_keys("default")
        keys_b = _get_api_keys("stage5")
        self.org_a = OrganizationTokenBucket("Org A", keys_a, max_tpm=max_tpm, refill_per_sec=refill_per_sec)
        self.org_b = OrganizationTokenBucket("Org B", keys_b, max_tpm=max_tpm, refill_per_sec=refill_per_sec)
        self.lock = threading.Lock()

    def acquire(
        self,
        estimated_tokens: float,
        preferred_org: str | None = None,
    ) -> tuple[OrganizationTokenBucket, str, float]:
        """Evaluate token availability across Org A and Org B and schedule request.

        Algorithm:
        1. Evaluate token availability across Org A and Org B (considering preferred_org if specified).
        2. If one org has immediate capacity for estimated_tokens, dispatch immediately (wait = 0).
        3. If neither has capacity, pick the org with the shortest wait time until sufficient refill.
        4. Sleep proactively before sending if wait > 0.
        5. Round-robin through keys in the selected org.

        Returns:
            (selected_bucket, selected_key, wait_seconds)
        """
        with self.lock:
            now = time.time()

            # Normalize preferred org target
            target_org = None
            if preferred_org:
                p = preferred_org.lower().strip()
                if "b" in p or "stage5" in p:
                    target_org = self.org_b
                elif "a" in p or "default" in p:
                    target_org = self.org_a

            cap_a = self.org_a.has_immediate_capacity_locked(now, estimated_tokens)
            cap_b = self.org_b.has_immediate_capacity_locked(now, estimated_tokens)

            chosen_org = None

            # Case 1: Preferred target org has immediate capacity
            if target_org is self.org_a and cap_a:
                chosen_org = self.org_a
            elif target_org is self.org_b and cap_b:
                chosen_org = self.org_b
            # Case 2: One of the orgs has immediate capacity
            elif cap_a and not cap_b:
                chosen_org = self.org_a
            elif cap_b and not cap_a:
                chosen_org = self.org_b
            elif cap_a and cap_b:
                # Both have immediate capacity:
                if target_org is not None:
                    chosen_org = target_org
                elif self.org_a.available_tokens >= self.org_b.available_tokens:
                    chosen_org = self.org_a
                else:
                    chosen_org = self.org_b
            else:
                # Case 3: Neither has immediate capacity -> Minimum Delay Scheduling
                wait_a = self.org_a.wait_time_locked(now, estimated_tokens)
                wait_b = self.org_b.wait_time_locked(now, estimated_tokens)

                if wait_a < wait_b:
                    chosen_org = self.org_a
                elif wait_b < wait_a:
                    chosen_org = self.org_b
                else:
                    # Tie break: prefer target if specified, otherwise Org A
                    chosen_org = target_org if target_org is not None else self.org_a

            wait_sec = chosen_org.wait_time_locked(now, estimated_tokens)
            key = chosen_org.reserve_locked(now, estimated_tokens)

            return chosen_org, key, wait_sec


_DISPATCHER = None
_DISPATCHER_LOCK = threading.Lock()


def get_dispatcher() -> DualOrgDispatcher:
    """Retrieve global singleton DualOrgDispatcher."""
    global _DISPATCHER
    with _DISPATCHER_LOCK:
        if _DISPATCHER is None:
            _DISPATCHER = DualOrgDispatcher()
        return _DISPATCHER


def _get_pool(pool_name="default"):
    """Backward-compatible pool getter returning the OrganizationTokenBucket."""
    disp = get_dispatcher()
    p = str(pool_name).lower() if pool_name else "default"
    if "stage5" in p or "org_b" in p or p == "b":
        return disp.org_b
    return disp.org_a


def estimate_tokens(messages: list[dict], max_tokens: int) -> int:
    """Proactively estimate token footprint: len(prompt)//3.2 + max_tokens."""
    prompt_len = 0
    for m in messages:
        if isinstance(m, dict):
            prompt_len += len(m.get("content") or "")
    est = int(prompt_len // 3.2) + int(max_tokens)
    return max(1, est)


def chat(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    preferred_org: str | None = None,
    response_format: dict | None = None,
    timeout: float = 120.0,
    pool_name: str | None = None,
    **kwargs,
) -> str:
    """Send a chat completion request with proactive pacing, rate-limiting, and failover.

    Standard library only. Zero disk caching.
    """
    _load_env_if_needed()
    base = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    chosen_model = model or os.environ.get("LLM_MODEL", "qwen/qwen3.8-27b")
    effective_max_tokens = max_tokens if max_tokens is not None else 1000

    body = {
        "model": chosen_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": effective_max_tokens,
    }
    if response_format:
        body["response_format"] = response_format

    # Backward compatibility with pool_name parameter
    if pool_name and not preferred_org:
        p = pool_name.lower()
        preferred_org = "org_b" if "stage5" in p or "b" in p else "org_a"

    estimated_tokens = estimate_tokens(messages, effective_max_tokens)
    dispatcher = get_dispatcher()
    max_retries = 25
    data = None
    chosen_org = None

    for attempt in range(max_retries):
        # Minimum Delay Cooperative Scheduling
        chosen_org, key, wait_sec = dispatcher.acquire(
            estimated_tokens=estimated_tokens,
            preferred_org=preferred_org,
        )

        if not key:
            raise RuntimeError("No API key available for LLM request")

        # Proactively sleep before sending if bucket needs refilling
        if wait_sec > 0:
            time.sleep(wait_sec)

        payload_bytes = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            base + "/chat/completions",
            data=payload_bytes,
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
                # Dynamic header synchronization
                chosen_org.update_from_headers(headers)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    pass

                # Check if Daily Quota (TPD) reached
                if "tokens per day" in err_text.lower() or "tpd" in err_text.lower():
                    print(f"  Daily quota (TPD) reached on {chosen_org.name}. Pausing for 180s...", file=sys.stderr)
                    chosen_org.set_cooldown(180.0)
                    continue

                # Dynamic calculation of retry delay without crude 60s freeze
                delay = _extract_retry_delay(err_text, dict(e.headers))
                wait = max(0.1, delay + 0.1)  # small 100ms safety cushion
                chosen_org.set_cooldown(wait)
                print(
                    f"  Rate-limited (429) on {chosen_org.name} ({chosen_model}). "
                    f"Dynamically waiting {wait:.2f}s before retry...",
                    file=sys.stderr,
                )
                time.sleep(wait)
                if attempt == max_retries - 1:
                    raise
                continue
            elif attempt < max_retries - 1:
                wait = min(2 ** attempt, 8.0)
                print(f"  LLM call HTTP {e.code} (attempt {attempt+1}/{max_retries}): {e}. Retrying in {wait:.1f}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            if attempt < max_retries - 1:
                wait = min(2 ** attempt, 8.0)
                print(f"  LLM call exception (attempt {attempt+1}/{max_retries}): {e}. Retrying in {wait:.1f}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise

    if data is None:
        raise RuntimeError("No response data received from LLM")

    # Optional usage logging only if explicitly requested via environment variable
    if os.environ.get("LOG_USAGE") == "1":
        usage = data.get("usage") or {}
        try:
            with open("usage.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "model": body["model"],
                    "org": chosen_org.name if chosen_org else "unknown",
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                }) + "\n")
        except Exception:
            pass

    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    if not content.strip() and msg.get("reasoning"):
        print("  Warning: LLM returned empty content with reasoning (increase max_tokens?)", file=sys.stderr)
    return content
