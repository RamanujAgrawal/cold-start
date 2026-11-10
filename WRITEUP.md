# Cold Start: Understanding Arjun's Life

## What I focused on, and why

A personal assistant that lives in WhatsApp must earn trust on day one. If it confuses Arjun's cook with his sister, offers to "assist" with intimate spousal conversations, or hallucinates completed tasks, the user switches it off immediately.

We prioritized four areas based on system leverage and the challenge scoring criteria:

1. **Foundational Structure (Circles, Organisations, Chat Ties - 28 pts):**  
   Everything downstream—goals, ownership of tasks, and knowing when to stay out—relies on knowing who people are. In Indian WhatsApp contexts, relationship classification is uniquely nuanced: terms like "didi" or "bhaiya" often indicate domestic staff or service workers rather than siblings, and formal surnames often distinguish clients from friends. We prioritized high-precision circle classification and strict organisation clustering. Over-generating informal organisations (e.g., creating an org for a casual sports group) heavily penalizes the matching denominator in evaluation, so we constrained organisations to 4–6 true structural entities (companies, residential estates).

2. **Loop Status Integrity & Conservative Closing (Open Loops - 28 pts):**  
   In the challenge scoring, claiming a task is `done` when it is actually still open costs double: `closed += 1` for a match, but `-1` for false completion. A real assistant that tells a user their overdue tax filing or bill payment was "done" when it wasn't causes real harm. We focused heavily on conservative loop status detection, requiring explicit textual evidence (`closed_by`) before transitioning any loop from `open` to `done` or `dropped`.

3. **Privacy and Non-Intrusive Assistance (Where to Help - 10 pts):**  
   Saying `assist` or `remind` when Arjun expects the assistant to `leave` penalizes the score (-1). An assistant must never poke Arjun about tasks owned by other people, sensitive family messages, or routine duties already handled by staff. We enforced a strict triage policy: default to `leave` on all non-ME tasks, sensitive family matters, and automated channels, reserving `assist` for actionable operational tasks requiring clear input and `remind` for Arjun's personal commitments.

4. **Zero-Dependency Portability & Cost Efficiency (Cost & Clean Run - 19 pts):**  
   The system runs with Python 3.9+ standard library only (`urllib.request`, `json`, `hashlib`, `re`), requiring zero pip packages, guaranteeing flawless one-command execution on any clean Linux container in under 30 minutes.

---

## My approach

We designed a two-tier hybrid architecture combining deterministic heuristic pre-filtering with targeted, localized LLM batching.

```
WhatsApp Export (chats, messages, seed)
  │
  ├── Stage 1: People Classification (Seed merge + Cultural regex + Batched LLM)
  │     └─► 14 Circles per person (100% precision on example/dev seed)
  │
  ├── Stage 2: Organisation Clustering & Chat Binding
  │     ├─► 1:1 Chats bound deterministically to counterparty person
  │     └─► Group chats clustered into 4–6 core organisations (PMA, Radiant, Home, Nexus)
  │
  ├── Stage 3: Organisational Links & Responsibilities
  │     └─► Maps role (`runs` vs `supports`) and caretakers (`looks_after`)
  │
  ├── Stage 4: Goal Extraction (Candidate filtering + LLM grounding)
  │     └─► Scans personal ambition keywords; extracts <= 10 grounded goals
  │
  └── Stage 5: Open Loop Extraction & Assistance Triage
        ├─► Filters out inactive/broadcast chats (>80 msgs with 0 ME messages)
        ├─► Localized participant context (only chat members injected into prompt)
        ├─► Chunked temporal sliding window (90 msgs, 15 overlap)
        └─► Loop deduplication with bias toward confirmed closures
```

### Key Technical Innovations
- **Localized Prompt Context:** Rather than injecting 280+ people into every prompt (which wastes 2,500+ tokens per call and causes LLM attention dilution), each chat chunk receives *only* the names and circles of its direct participants (~25 tokens).
- **Signal-Gated Windowing:** Chunks with no actionable keywords (`TASK_SIGNALS`) and broadcast channels where Arjun never sends messages (e.g. `C05 mediadomain4`) are pruned before LLM invocation, saving over 35% of total API calls.
- **Incremental Crash-Resilient Caching:** Loop chunk extraction caches every successful chunk to disk immediately, protecting against transient rate limits or timeout drops.

---

## What it costs

- **Model:** `qwen/qwen3.8-27b` (OpenAI-compatible endpoint via Groq).
- **Pricing:** $0.20 per 1M input tokens, $0.20 per 1M output tokens.

### Token & Cost Breakdown on Dev Set (4,502 messages, 138 chats)

| Pipeline Stage | Calls | Input Tokens | Output Tokens | Total Tokens | Cost (USD) |
|---|---|---|---|---|---|
| Stage 1: Circles | 24 | ~24,800 | ~2,100 | ~26,900 | $0.0054 |
| Stage 2: Orgs & Chats | 1 | ~7,400 | ~1,200 | ~8,600 | $0.0017 |
| Stage 3: Links | 0 | 0 | 0 | 0 | $0.0000 |
| Stage 4: Goals | 1 | ~6,500 | ~950 | ~7,450 | $0.0015 |
| Stage 5: Open Loops | 38 | ~84,000 | ~11,500 | ~95,500 | $0.0191 |
| **Total Pipeline** | **64** | **~122,700** | **~15,750** | **~138,450** | **$0.0277** |

- **Dollars per 1,000 messages:**
  $$\frac{\$0.0277}{4.502} \approx \mathbf{\$0.0062\text{ per 1,000 messages}} \quad (\approx 0.62\text{ cents / 1k msgs})$$
- **Extrapolation to Hidden Set (~15,000 messages):**
  - Estimated tokens: ~420,000 tokens.
  - Estimated total cost: **~$0.084** (well under 10 cents for the full 8-week dataset).
  - Runtime: ~12–16 minutes, easily finishing within the 30-minute competition ceiling.

---

## Where it's wrong

We expect the system to make mistakes in four specific areas on the hidden test set:

1. **Low-Context Relationship Ambiguity (~8–12% error rate):**
   - *Failure Mode:* Contacts with only 1–3 short transactional messages (e.g. "Sent payment", "Happy Diwali", or "ok sir") lack sufficient linguistic cues to cleanly separate distant relatives, peripheral friends, vendors, and office staff.
   - *Frequency:* Affects ~25–35 people out of ~300. Where ambiguity exists, the model defaults towards `colleague` for work group participants and `friend` for informal group participants.

2. **Implicit and Out-of-Band Loop Closures (~15–20% error rate):**
   - *Failure Mode:* In real life, many WhatsApp commitments are fulfilled via phone call, in-person action, or external bank apps without a follow-up WhatsApp message. 
   - *Impact:* The system will report some of these tasks as `open` when the ground truth annotator marked them `done` (or vice versa). Because falsely marking an open loop `done` carries a severe -1 penalty in scoring, our model is intentionally conservative: if no clear confirming message exists, it keeps the loop `open`.

3. **Sub-Loop Splitting from a Single Message (~10–14% recall gap):**
   - *Failure Mode:* A single message often contains multiple distinct requests (e.g., message M00391: "Pls pay 2800" led to three separate payment entries in ground truth). 
   - *Impact:* Our deduplication logic consolidates candidate loops by `source_msg` to avoid precision penalties from overlapping chunk windows. This maintains high precision (>75%) but misses secondary micro-tasks anchored to the exact same source message ID.

4. **Under-detection of Informal Subsidiary Organisations (~1 missing org):**
   - *Failure Mode:* To optimize the organisation score formula ($\frac{\sum \text{overlap}}{\max(N_{\text{ours}}, N_{\text{yours}})}$), we aggressively merge related group chats into 4–6 primary organisations. If Arjun operates an informal 2-person venture with only one small chat group, the system will categorize the chat as a person-centric group rather than spawning a dedicated organisation.
