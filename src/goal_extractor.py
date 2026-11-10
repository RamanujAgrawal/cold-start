"""Extract goals from chat data."""
import json
import sys
from .llm_client import chat
from .data_loader import format_messages_window, chunk_messages


VALID_AREAS = ["self", "spouse", "kids", "parents", "work", "friends", "experiences", "finances", "health", "home"]
VALID_TYPES = ["habit", "milestone", "experience"]
VALID_HORIZONS = ["3mo", "1yr", "3yr", "5yr"]


SYSTEM_PROMPT = """You are analyzing WhatsApp messages to identify the LONG-TERM goals that the user (ME) is working towards.

A goal is something the user is actively working toward over weeks or months, visible in conversations.

Types:
- habit: a rhythm or recurring practice (e.g., "run 3 times a week", "morning yoga daily")
- milestone: a specific target with a timeline (e.g., "10 paying clients by March", "complete house renovation by December")
- experience: something to plan and do (e.g., "family trip to Coorg in December", "attend fashion week")

Each goal has:
- title: clear, specific description
- area: self, spouse, kids, parents, work, friends, experiences, finances, health, home
- type: habit, milestone, experience
- horizon: 3mo, 1yr, 3yr, 5yr (how far out the goal stretches)
- evidence: list of message IDs that show this goal

CRITICAL RULES:
- A goal is NOT a one-time task like "book a gas cylinder" or "prepare dinner"
- A goal is NOT a single todo item — it's a sustained direction or target
- Only report goals that have CLEAR, STRONG evidence in the messages
- When in doubt, DON'T include it — invented goals cost points
- Fewer confident goals are better than many uncertain ones
- Max 10 goals, but 3-6 well-evidenced ones is ideal
- Each goal needs at least 1 evidence message ID
- Goals from seed.json should NOT be repeated

Return ONLY a JSON array of goal objects. No markdown."""


def extract_goals(chats, messages, indices, seed, circles):
    """Extract up to 10 goals from the chat data."""
    # First, create summaries of the most active chats
    chat_summaries = []
    
    # Sort chats by message count
    chat_msg_counts = [(cid, len(msgs)) for cid, msgs in indices["msgs_by_chat"].items()]
    chat_msg_counts.sort(key=lambda x: x[1], reverse=True)
    
    # Focus on the top chats and all chats involving ME
    relevant_msgs = []
    for m in messages:
        body = (m.get("body") or "").strip()
        if not body or body == "[photo]":
            continue
        relevant_msgs.append(m)
    
    # Chunk the relevant messages for goal analysis
    # Focus on ME's messages and responses — these reveal goals
    me_conversations = []
    for cid, chat_msgs in indices["msgs_by_chat"].items():
        me_msgs = [m for m in chat_msgs if m["sender_id"] == "ME" and (m.get("body") or "").strip()]
        if me_msgs:
            c = indices["chat_by_id"][cid]
            for m in me_msgs[:10]:  # Top 10 messages from ME per chat
                # Include the message before ME's for context
                idx_in_chat = next((i for i, cm in enumerate(chat_msgs) if cm["id"] == m["id"]), 0)
                context_start = max(0, idx_in_chat - 2)
                context_end = min(len(chat_msgs), idx_in_chat + 2)
                context_msgs = chat_msgs[context_start:context_end]
                me_conversations.extend(context_msgs)
    
    # Deduplicate and sort
    seen_ids = set()
    unique_msgs = []
    for m in me_conversations:
        if m["id"] not in seen_ids:
            seen_ids.add(m["id"])
            unique_msgs.append(m)
    unique_msgs.sort(key=lambda m: m["ts"])
    
    # Take a representative sample (limit to ~80 high-signal messages for cost/payload)
    if len(unique_msgs) > 80:
        # Prioritize messages with goal-related keywords
        keywords = {"goal", "target", "plan", "trip", "run", "marathon", "holiday", "december", "march", "january", "client", "crore", "lakh", "revenue", "health", "fitness", "yoga", "school", "house", "build", "hire"}
        scored_msgs = []
        for m in unique_msgs:
            score = 0
            text = (m.get("body") or "").lower()
            if m["sender_id"] == "ME":
                score += 2
            if any(k in text for k in keywords):
                score += 3
            scored_msgs.append((score, m))
        scored_msgs.sort(key=lambda x: (x[0], x[1]["ts"]), reverse=True)
        unique_msgs = [x[1] for x in scored_msgs[:80]]
        unique_msgs.sort(key=lambda m: m["ts"])
    
    # Format for LLM
    msgs_text = format_messages_window(unique_msgs, indices["person_names"])
    
    # Seed goals context
    seed_goals = seed.get("goals", [])
    seed_context = ""
    if seed_goals:
        seed_context = "\nAlready known goals (don't repeat these):\n"
        for g in seed_goals:
            seed_context += f"  - {g['title']} ({g['area']}, {g['type']}, {g['horizon']})\n"
    
    # Known circles for context
    circle_context = "\nKey people:\n"
    for pid in sorted(circles.keys())[:20]:
        name = indices["person_names"].get(pid, pid)
        circle_context += f"  {pid} ({name}): {circles[pid]}\n"
    
    me_name = indices.get("me_name", "User")
    prompt = f"""Analyze these WhatsApp messages to identify {me_name}'s (ME) goals.

{seed_context}
{circle_context}

MESSAGES (format: [msg_id] sender: text):
{msgs_text}

Find up to 10 goals that {me_name} is actively working towards. Each goal needs:
- title: specific description
- area: one of {VALID_AREAS}
- type: one of {VALID_TYPES}
- horizon: one of {VALID_HORIZONS}
- evidence: list of message IDs (at least 1) from the messages above

IMPORTANT: Only report goals with clear evidence. Don't invent goals.
If a goal matches a seeded goal, skip it.

Return ONLY a JSON array. No markdown."""

    try:
        response = chat(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=800,
        )
        
        response = response.strip()
        if response.startswith("```"):
            response = response.split("\n", 1)[1]
            response = response.rsplit("```", 1)[0]
        
        goals = json.loads(response)
        
        # Validate
        msg_ids = {m["id"] for m in messages}
        valid_goals = []
        for g in goals:
            if not g.get("title"):
                continue
            if g.get("area") not in VALID_AREAS:
                continue
            if g.get("type") not in VALID_TYPES:
                continue
            if g.get("horizon") not in VALID_HORIZONS:
                continue
            # Validate evidence
            evidence = [e for e in (g.get("evidence") or []) if e in msg_ids]
            if not evidence:
                continue
            g["evidence"] = evidence
            valid_goals.append(g)
        
        return valid_goals[:10]
    
    except Exception as e:
        print(f"  Warning: Goal extraction failed: {e}", file=sys.stderr)
        return []
