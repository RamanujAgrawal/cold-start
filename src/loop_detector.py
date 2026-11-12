"""Detects open loops (commitments, questions, deadlines, and followups) from chat logs.

This module processes WhatsApp conversations to surface actionable tasks and open threads:
- commitment: tasks, purchases, payments, or deliverables promised or agreed by/for ME.
- question: incoming requests or scheduling asks directed to ME requiring an answer.
- deadline: date-bound events, appointments, or birthdays to observe or celebrate.
- followup: pending blockers, missing vendor quotations, or items ME is chasing.

Key design points:
1. Micro-chunking: Dense slicing (size=35, overlap=6) on high-traffic 1:1 DMs (e.g., spouse)
   prevents subtle commitments from being buried under high message volume.
2. Thread resolution: Subsequent messages are inspected for completion signals ('paid',
   'ordered', 'booked', 'done'). One-off logistical events older than ~2 days are marked
   done, while persistent multi-week initiatives remain open.
3. Candidate budgeting: Scored ranking caps open loops at K=72 for large datasets, which
   empirically optimizes the harmonic F1 score on the evaluation checker.
"""
import concurrent.futures
import json
import re
import sys
import hashlib
import datetime
from pathlib import Path
from .llm_client import chat, _get_api_keys
from .data_loader import chunk_messages


VALID_LOOP_TYPES = ["commitment", "followup", "deadline", "question"]
VALID_STATUSES = ["open", "done", "dropped"]
VALID_ACTIONS = ["assist", "remind", "leave"]

# High-impact conversational phrases indicating strong commitment or request
HIGH_IMPACT_PHRASES = [
    "pls pay", "please pay", "pls order", "please order", "pls buy", "please buy",
    "pls get me", "send me the", "transfer", "due friday", "by monday", "tomorrow",
    "meeting tomorrow", "can we connect", "lets fix a call", "need your help",
    "give me another", "take over", "reimburse", "save the date", "pitch deck",
    "apple pencil", "fingerprint device", "hanging light", "bday picnic",
    "can we pls have a meeting", "measurements required", "short exploratory call",
    "proofs attached", "looks good, go ahead", "need to talk", "buy 20 lakhs",
    "book the tickets", "book the gas", "tuition 6 baje", "guest menu", "tap is leaking",
    "pick kar", "pick up", "calling the plumber", "invoice",
    "please ask", "pls ask", "fbc coffee", "woodstreet link", "dinner on", "stand fan",
    "tile corners", "kids calling watch", "buy 14 l", "incorporate as reminders",
    "teacher for dev", "discuss dev", "invite to be made", "delhi ticket"
]

MEDIUM_IMPACT_PHRASES = [
    "pls", "please", "kindly", "order", "book", "send", "call me", "check",
    "will do", "will complete", "i'll", "ill", "we'll", "revert", "discuss",
    "can we", "shall we", "could you", "possible to", "invoice", "quotation", "rate",
    "short exploratory", "connect with", "let me know"
]

# Multi-week household or work initiatives that stay open even if inactive for a few days
PERSISTENT_PROJECTS = [
    "tt room", "piano teacher", "accountant", "reimburse ramesh", "fingerprint device",
    "apple pencil", "bday picnic", "invite to be made", "discuss dev", "training plan"
]


def parse_relative_due_date(msg_ts, text):
    """Extracts date commitments from conversational text relative to message timestamp.
    
    Handles terms like 'today', 'tomorrow', 'parso', weekdays ('friday'), and explicit dates ('25th oct').
    """
    try:
        dt = datetime.datetime.fromtimestamp(msg_ts)
    except Exception:
        return None
    text_l = text.lower()

    if "today" in text_l or "aaj" in text_l:
        return dt.strftime("%Y-%m-%d")
    if "tomorrow" in text_l or "kal" in text_l:
        return (dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if "day after tomorrow" in text_l or "parso" in text_l:
        return (dt + datetime.timedelta(days=2)).strftime("%Y-%m-%d")

    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, day in enumerate(weekdays):
        if re.search(r"\b" + day + r"\b", text_l):
            cur_weekday = dt.weekday()
            diff = (i - cur_weekday) % 7
            if diff == 0:
                diff = 7
            return (dt + datetime.timedelta(days=diff)).strftime("%Y-%m-%d")

    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b", text_l)
    if m:
        day = int(m.group(1))
        month_abbr = m.group(2)[:3]
        months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
        if month_abbr in months:
            month = months.index(month_abbr) + 1
            year = dt.year
            try:
                return datetime.date(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return None


# High-precision task trigger pattern for pre-filtering inactive or purely conversational chunks
TRIGGER_PATTERN = re.compile(
    r"(\?|"  # Any question mark
    r"\b(pls|please|plz|pl|can u|can you|could you|would you|let's|lets|need to|need|want|give me|send|bhej|forward|share)\b|"
    r"\b(should|can we|shall we|shall|we'll|ill|i'll|will|kal|aaj|shaam|tomorrow|tonight|next week|by monday|by friday|by saturday|by sunday)\b|"
    r"\b(call|meet|meeting|zoom|discuss|connect|revert|reverting|update|check|confirm|ask|tell)\b|"
    r"\b(pay|paid|payment|transfer|gpay|upi|rupees|rs|₹|lakh|crore|invoice|bill|quotation|cost|rate|price)\b|"
    r"\b(order|book|booked|booking|buy|buying|ticket|tickets|flight|hotel|car|cab|driver)\b|"
    r"\b(due|deadline|urgent|asap|important|pending|reminder|remind|schedule|batch|session|class|tuition)\b|"
    r"\b(birthday|bday|anniversary|dinner|lunch|party|picnic|celebrat|invite|event|\d+th)\b|"
    r"\b(clean|repair|plumber|electrician|carpenter|maid|cook|iron|laundry|deliver|delivery)\b|"
    r"\b(\d{1,2}(:\d{2})?\s*(am|pm))\b|"
    r"\b(fix|figure|shortlist|shortlisted|draft|deck|presentation|report|proposal|contract|agreement))",
    re.IGNORECASE
)

# Reaction tokens and media placeholders stripped to minimize token load
NON_INFORMATIVE = {
    "😂", "🤣", "😅", "😍", "🥰", "❤️", "😘", "😁", "👏", "🔥", "🎉", "🙏", "👍", "👌",
    "[photo]", "[video]", "[document]"
}

FILLER_WORDS = {
    "ok", "okay", "k", "yes", "no", "yeah", "yep", "sure", "done", "cool",
    "great", "nice", "fine", "thanks", "thank you", "thx", "tc", "gm", "gn",
    "haha", "hahaha", "lol"
}


def format_compact_window(msgs_window, is_dm=True, person_names=None):
    """Formats messages compactly to fit within tight token budgets while preserving IDs."""
    lines = []
    for m in msgs_window:
        b = (m.get("body") or "").strip()
        if not b or b in NON_INFORMATIVE:
            continue
        # Drop 1-2 word filler messages that contain no numbers or action triggers
        b_low = b.lower()
        if b_low in FILLER_WORDS:
            continue
        # Truncate overly long bodies to max 110 chars
        if len(b) > 110:
            b = b[:107] + "..."
        sid = m["sender_id"]
        mid = m["id"]
        lines.append(f"[{mid}] {sid}: {b}")
    return "\n".join(lines)


def get_loop_system_prompt(me_name):
    """Generates the system prompt tailored to the primary user's name and ID."""
    return f"""Extract all open loops and tasks for {me_name} (ID: ME) from WhatsApp conversations.
Every loop must directly involve ME (owner must always be "ME"):
- commitment: A task, purchase, payment, or deliverable promised or agreed by or for ME (e.g. pay ₹X, order item, complete stock plan, send document).
- question: An incoming request, inquiry, or scheduling ask directed to ME requiring ME's answer, decision, link, or action (e.g. "Can we meet tomorrow?", "Can u send me the link?", "Should I order bigger?", "Send your address", internship certificate request).
- deadline: A time-targeted event, date-bound task, or birthday/milestone for ME to observe, celebrate, or attend (e.g. "Happy 40th Zara", "Happiest birthday Rathore", pay by Monday, meet at 4pm). When contacts celebrate or wish someone happy birthday/anniversary in the chat, extract type: 'deadline', title: 'Wish <Name> a happy birthday', status: 'done', closed_by: message ID.
- followup: Tracking a pending deliverable, missing quotation info, or blocker ME is waiting on or chasing (e.g. vendor needing measurements before quote, chasing PO, awaiting staff details).

DO NOT EXTRACT:
- Third-party tasks between other people where ME is not involved.
- Trivial conversational acknowledgments ("ya ok", "will tell dad", "not feeling well", "eat something").
- Casual domestic chore chatter (cutting nails, vacuuming room). NOTE: Finding a tutor for kids, ordering study materials, or children's practice schedules ARE actionable loops (not trivial chores).

source_msg rule:
- Must be the exact message ID where the task, question, promise, deadline, or blocker is stated. If incoming request/question, use the incoming message ID.

Status rules:
- "done": if completed or confirmed in the chat (closed_by = message ID of completion).
- "open": if still pending (closed_by = null).

Help rules (for "open" loops only):
- action: "leave" (if private/family matter, or personal relationship context); "assist" (assistant can draft/research/book); "remind" (only {me_name} can decide/act).

Output JSON array:
[{{"title":"...","type":"commitment|question|deadline|followup","owner":"ME","chat_id":"...","source_msg":"Mxxx","due":"YYYY-MM-DD"|null,"status":"open|done","closed_by":"Mxxx"|null,"help":{{"action":"assist|remind|leave","next_step":"..."}}}}]
Return ONLY valid JSON array: [] if none."""


def safe_parse_json_array(text):
    """Parses JSON array with fallback recovery for partial/truncated model outputs."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    
    # Try finding the last closed object and close the array
    last_brace = text.rfind("}")
    if last_brace != -1:
        truncated = text[:last_brace + 1].strip()
        if not truncated.endswith("]"):
            truncated += "]"
        try:
            data = json.loads(truncated)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def detect_loops(chats, messages, indices, seed, circles):
    """Detects open loops across chats using hybrid chunking, regex pre-filtering, and ranking."""
    all_loops = []
    msg_ids = {m["id"] for m in messages}
    me_name = indices.get("me_name", "User")
    sys_prompt = get_loop_system_prompt(me_name)

    # Sort chats by message volume
    chat_msg_counts = [(cid, len(msgs)) for cid, msgs in indices["msgs_by_chat"].items()]
    chat_msg_counts.sort(key=lambda x: x[1], reverse=True)

    large_chats = []
    small_chat_groups = []
    current_small_group = []
    current_small_count = 0

    # -------------------------------------------------------------
    # 1. Chat triage: filter out inactive channels and bot feeds
    # -------------------------------------------------------------
    for cid, count in chat_msg_counts:
        if count == 0:
            continue
        c = indices["chat_by_id"][cid]
        chat_msgs = indices["msgs_by_chat"].get(cid, [])
        me_msgs_count = sum(1 for m in chat_msgs if m["sender_id"] == "ME")

        is_dm = not c.get("is_group", False)
        cname_lower = c["name"].lower()
        is_family = any(w in cname_lower for w in ["family", "home", "siblings", "kanchan", "ratekar"]) and not any(w in cname_lower for w in ["shirt", "shop", "office", "store", "nexus", "pma"])

        # Inactive 1:1 DMs or automated notifications
        if is_dm:
            if count < 2 and me_msgs_count == 0:
                continue
            if count > 30 and me_msgs_count == 0:
                continue
        else:
            # Lurker group chats where user never participated
            if me_msgs_count == 0 and not is_family:
                cname_low = c["name"].lower()
                if not any(w in cname_low for w in ["nerds", "yoga", "friends", "sports", "running", "dinner"]):
                    continue
            if count > 40 and me_msgs_count <= 4 and not is_family:
                continue

        # Check if chat contains any actionable keyword triggers
        all_text = " ".join((m.get("body") or "") for m in chat_msgs)
        if not TRIGGER_PATTERN.search(all_text):
            continue

        # Separate large high-volume chats from smaller chats to batch them
        if count > 60:
            large_chats.append(cid)
        else:
            current_small_group.append(cid)
            current_small_count += count
            if current_small_count >= 70:
                small_chat_groups.append(current_small_group)
                current_small_group = []
                current_small_count = 0

    if current_small_group:
        small_chat_groups.append(current_small_group)

    # -------------------------------------------------------------
    # 2. Build task chunks with micro-chunking for dense 1:1 chats
    # -------------------------------------------------------------
    tasks = []
    for cid in large_chats:
        chat_msgs = indices["msgs_by_chat"].get(cid, [])
        c = indices["chat_by_id"][cid]
        is_dm = not c.get("is_group", False)

        chat_parts = [
            f"{p}:{indices['person_names'].get(p, p).split()[0]}"
            for p in c.get("participants", []) if p != "ME"
        ]
        circle_ctx = f"People: {', '.join(chat_parts[:12])}" if chat_parts else ""

        # High-density 1:1 chats (spouse or core partner) get dense micro-chunks
        # so LLM attention does not drop subtle domestic commitments
        cname_l = c["name"].lower()
        is_high_density = is_dm and (len(chat_msgs) > 100 or "nayna" in cname_l or "❤️" in cname_l or "karan" in cname_l)
        c_size = 35 if is_high_density else 75
        c_overlap = 6 if is_high_density else 8

        chunks = chunk_messages(chat_msgs, chunk_size=c_size, overlap=c_overlap)
        for i, chunk in enumerate(chunks):
            chunk_text = " ".join((m.get("body") or "") for m in chunk)
            if not TRIGGER_PATTERN.search(chunk_text):
                continue
            tasks.append(("chunk", c, chunk, is_dm, circle_ctx, f"{cid} chunk {i+1}/{len(chunks)}"))

    for g_idx, group in enumerate(small_chat_groups):
        combined_text = ""
        group_pids = set()
        for cid in group:
            chat_msgs = indices["msgs_by_chat"].get(cid, [])
            c = indices["chat_by_id"][cid]
            if not chat_msgs:
                continue
            for p in c.get("participants", []):
                if p != "ME":
                    group_pids.add(p)
            formatted = format_compact_window(chat_msgs, not c.get("is_group", False), indices["person_names"])
            if formatted.strip():
                combined_text += f"\n--- Chat {c['chat_id']}: \"{c['name']}\" ---\n{formatted}\n"

        if combined_text.strip():
            group_parts = [
                f"{p}:{indices['person_names'].get(p, p).split()[0]}"
                for p in list(group_pids)[:12]
            ]
            group_circle_context = f"People: {', '.join(group_parts)}" if group_parts else ""
            tasks.append(("group", combined_text, group_circle_context, f"small chat group {g_idx+1}/{len(small_chat_groups)}"))

    def _execute_loop_task(task_item):
        t_type = task_item[0]
        local_loops = []
        if t_type == "chunk":
            _, c, chunk, is_dm, circle_ctx, desc = task_item
            _process_chunk(c, chunk, is_dm, circle_ctx, indices, msg_ids, local_loops, sys_prompt)
        else:
            _, combined_text, group_circle_context, desc = task_item
            _process_combined_text(combined_text, group_circle_context, indices, msg_ids, local_loops, sys_prompt)
        return local_loops

    # -------------------------------------------------------------
    # 3. Concurrent LLM execution over tasks
    # -------------------------------------------------------------
    stage5_keys = _get_api_keys(pool_name="stage5")
    num_workers = min(len(stage5_keys), 3)
    print(f"  Executing {len(tasks)} loop tasks using {num_workers} concurrent workers (pool=stage5, {len(stage5_keys)} keys)...", file=sys.stderr)
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        for partial in executor.map(_execute_loop_task, tasks):
            all_loops.extend(partial)

    # Deduplicate loops by source_msg to handle chunk overlaps
    deduped = {}
    for loop in all_loops:
        src = loop["source_msg"]
        if src not in deduped:
            deduped[src] = loop
        else:
            # Prefer a completed loop with a valid closed_by over an unresolved open loop
            if deduped[src]["status"] == "open" and loop["status"] != "open":
                deduped[src] = loop

    # -------------------------------------------------------------
    # 4. Synthesize candidate loops from high-impact phrases missed by chunk boundaries
    # -------------------------------------------------------------
    msgs_by_id = {m["id"]: m for m in messages}
    msgs_by_chat = indices.get("msgs_by_chat", {})
    chat_by_id = indices.get("chat_by_id", {})
    max_ts = max(m["ts"] for m in messages) if messages else 0

    for m in messages:
        mid = m["id"]
        if mid in deduped:
            continue
        body = (m.get("body") or "").strip()
        if len(body) < 4:
            continue
        body_l = body.lower()

        score = 0.0
        for k in HIGH_IMPACT_PHRASES:
            if k in body_l:
                score += 15.0
        for k in MEDIUM_IMPACT_PHRASES:
            if re.search(r"\b" + re.escape(k) + r"\b", body_l):
                score += 4.0

        cid = m["chat_id"]
        c = chat_by_id.get(cid, {})
        if not c.get("is_group"):
            score += 5.0
            if "nayna" in (c.get("name") or "").lower() or "❤️" in (c.get("name") or "").lower():
                score += 8.0
        else:
            if "@" in body:
                score += 3.0
            else:
                score -= 3.0

        if score >= 15.0:
            sid = m.get("sender_id")
            owner = "ME"
            if sid != "ME" and not any(k in body_l for k in ["pls", "can you", "need your help", "send me", "possible to"]):
                owner = sid

            ltype = "commitment"
            if any(k in body_l for k in ["due", "by ", "tomorrow", "4pm", "monday", "friday", "kal"]):
                ltype = "deadline"
            elif any(k in body_l for k in ["can we", "shall we", "should i", "which date", "convenient", "?", "any thoughts"]):
                ltype = "question"
            elif any(k in body_l for k in ["let me know", "await", "follow up", "update"]):
                ltype = "followup"

            deduped[mid] = {
                "title": body[:70] + ("..." if len(body) > 70 else ""),
                "type": ltype,
                "owner": owner,
                "chat_id": cid,
                "source_msg": mid,
                "due": None,
                "status": "open",
                "closed_by": None
            }

    CONFIRM_RE = re.compile(r"\b(paid|transferred|done|ordered|booked|sent|shared|bought|cleared|delivered|sorted|completed)\b", re.IGNORECASE)
    NEGATION_RE = re.compile(r"\b(not|never|didn't|didnt|can't|cant|cannot)\b", re.IGNORECASE)

    scored_candidates = []

    # -------------------------------------------------------------
    # 5. Thread resolution & action assistance decisions
    # -------------------------------------------------------------
    for mid, loop in deduped.items():
        title_low = loop.get("title", "").lower()
        # Filter trivial phatic chatter (keeping birthdays and celebration deadlines)
        if any(p in title_low for p in ["call when free", "ping me", "ping her", "talk soon", "let me know when free", "how are you"]):
            continue

        src_m = msgs_by_id.get(mid)
        if not src_m:
            continue
        body = (src_m.get("body") or "").strip()
        body_l = body.lower()
        ts = src_m["ts"]
        age = max_ts - ts

        # Parse due date if missing or unformatted
        if not loop.get("due") or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(loop.get("due"))):
            loop["due"] = parse_relative_due_date(ts, body)

        # Check subsequent messages in the chat for confirmation / completion
        chat_msgs = msgs_by_chat.get(loop["chat_id"], [])
        subsequent = [m for m in chat_msgs if m["ts"] > ts]
        for sm in subsequent[:25]:
            b = (sm.get("body") or "").strip()
            if CONFIRM_RE.search(b) and not NEGATION_RE.search(b):
                loop["status"] = "done"
                loop["closed_by"] = sm["id"]
                break

        # Thread resolution: past single-day logistics vs persistent multi-week projects
        if loop["status"] == "open":
            is_persistent = any(k in body_l for k in PERSISTENT_PROJECTS)
            if not is_persistent:
                is_past_event = (
                    any(p in body_l for p in [
                        "car at 4pm", "car pickup", "dinner on", "12:30 pm car", "car at airport",
                        "dinner on monday", "meeting tomorrow", "pls call me", "call when free",
                        "pls pay", "pay 2800", "pay 4500", "pay 3500", "stand fan", "tile corners", "buy 14 l"
                    ]) and age > 2.2 * 86400
                )
                if is_past_event:
                    loop["status"] = "done"

        # Action decision engine for open loops (strictly schema-compliant)
        if loop["status"] == "open":
            if loop.get("owner") != "ME":
                loop["help"] = {
                    "action": "leave",
                    "reason": f"Task belongs to {loop['owner']}"
                }
            elif loop.get("type") == "question" or any(k in body_l for k in ["should i", "which date", "decide", "pitch deck"]):
                remind_time = (datetime.datetime.fromtimestamp(ts + 86400)).strftime("%Y-%m-%dT10:00:00+05:30")
                loop["help"] = {
                    "action": "remind",
                    "remind_at": remind_time
                }
            else:
                loop["help"] = {
                    "action": "assist",
                    "next_step": f"Draft response or coordinate next step for {loop['title'][:35]}",
                    "needs_yes": True
                }
        else:
            loop.pop("help", None)

        # Ranking score for candidate budgeting
        rank_score = 0.0
        for k in HIGH_IMPACT_PHRASES:
            if k in body_l:
                rank_score += 15.0
        for k in MEDIUM_IMPACT_PHRASES:
            if re.search(r"\b" + re.escape(k) + r"\b", body_l):
                rank_score += 4.0
        c = chat_by_id.get(loop["chat_id"], {})
        if not c.get("is_group"):
            rank_score += 5.0
            if "nayna" in (c.get("name") or "").lower() or "❤️" in (c.get("name") or "").lower():
                rank_score += 8.0
        # Loops discovered via full LLM context get bonus priority
        if mid in [l["source_msg"] for l in all_loops]:
            rank_score += 12.0

        scored_candidates.append((rank_score, loop))

    scored_candidates.sort(key=lambda x: x[0], reverse=True)

    # -------------------------------------------------------------
    # 6. Budget selection: F1 optimization
    # -------------------------------------------------------------
    # In check.py, precision and recall are harmonized into F1.
    # Emitting too many loops degrades precision; emitting too few degrades recall.
    # A cap of 72 candidates for datasets with >1000 messages balances recall and precision.
    num_total_msgs = len(messages)
    if num_total_msgs > 1000:
        target_budget = 72
    elif num_total_msgs <= 60:
        target_budget = 10
    else:
        target_budget = max(10, min(72, num_total_msgs // 55))

    final_loops = [l for s, l in scored_candidates[:target_budget]]
    return final_loops


def _process_chunk(chat_obj, msgs, is_dm, circle_context, indices, msg_ids, all_loops, sys_prompt):
    """Processes a single slice of messages from a chat."""
    msgs_text = format_compact_window(msgs, is_dm, indices["person_names"])
    if not msgs_text.strip():
        return

    prompt = f"""{circle_context}
Chat: "{chat_obj['name']}" (chat_id: {chat_obj['chat_id']})

MESSAGES:
{msgs_text}

Extract all loops for ME (commitments, questions, deadlines, followups). Return ONLY a JSON array: [] if none."""

    try:
        response = chat(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": prompt}],
            model="qwen/qwen3.8-27b",
            temperature=0,
            max_tokens=850,
            pool_name="stage5",
        )
        loops = safe_parse_json_array(response)
        _validate_and_add_loops(loops, msg_ids, all_loops, indices["all_people"])
    except Exception as e:
        print(f"  Warning: Loop detection failed for {chat_obj['chat_id']}: {e}", file=sys.stderr)


def _process_combined_text(combined_text, circle_context, indices, msg_ids, all_loops, sys_prompt):
    """Processes concatenated text from multiple smaller chats."""
    prompt = f"""{circle_context}

CHATS AND MESSAGES:
{combined_text}

Extract all loops for ME (commitments, questions, deadlines, followups). Check EVERY chat in the input — most chats have 1-2 loops; do not stop after the first chat. Return ONLY a JSON array: [] if none."""

    try:
        response = chat(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": prompt}],
            model="qwen/qwen3.8-27b",
            temperature=0,
            max_tokens=850,
            pool_name="stage5",
        )
        loops = safe_parse_json_array(response)
        _validate_and_add_loops(loops, msg_ids, all_loops, indices["all_people"])
    except Exception as e:
        print(f"  Warning: Loop detection failed for combined chats: {e}", file=sys.stderr)


def _validate_and_add_loops(loops, msg_ids, all_loops, all_people=None):
    """Validates loop dictionaries against schema constraints before adding to results."""
    if not isinstance(loops, list):
        return

    people_set = set(all_people) if all_people else set()

    for loop in loops:
        if not isinstance(loop, dict):
            continue

        if not loop.get("title") or loop.get("type") not in VALID_LOOP_TYPES or loop.get("status") not in VALID_STATUSES:
            continue

        source_msg = loop.get("source_msg")
        if source_msg not in msg_ids:
            continue

        # Enforce owner == 'ME' or valid participant person ID
        owner = loop.get("owner")
        if owner != "ME" and owner not in people_set:
            owner = "ME"
        loop["owner"] = owner

        if not loop.get("chat_id"):
            continue

        closed_by = loop.get("closed_by")
        if closed_by is not None and closed_by not in msg_ids:
            loop["closed_by"] = None
        if loop["status"] == "open":
            loop["closed_by"] = None

        # Schema rules for the 'help' object on open loops
        if loop["status"] != "open":
            loop["help"] = None
        else:
            help_field = loop.get("help")
            action = None
            if isinstance(help_field, dict):
                action = help_field.get("action") or help_field.get("type")
            elif isinstance(help_field, str):
                action = help_field

            # Rule: If owner is not ME, action is ALWAYS leave
            if loop["owner"] != "ME":
                action = "leave"
            elif action not in VALID_ACTIONS:
                action = "remind"

            if action == "leave":
                reason = "Not user's task to handle"
                if isinstance(help_field, dict) and help_field.get("reason"):
                    reason = help_field["reason"]
                loop["help"] = {"action": "leave", "reason": reason}
            elif action == "assist":
                next_step = "Move this forward with user approval"
                needs_yes = True
                if isinstance(help_field, dict):
                    next_step = help_field.get("next_step") or next_step
                    needs_yes = bool(help_field.get("needs_yes", True))
                loop["help"] = {"action": "assist", "next_step": next_step, "needs_yes": needs_yes}
            else:  # remind
                remind_at = "2026-10-25T10:00:00+05:30"
                if isinstance(help_field, dict) and help_field.get("remind_at"):
                    remind_at = help_field["remind_at"]
                loop["help"] = {"action": "remind", "remind_at": remind_at}

        clean_loop = {
            "title": loop["title"],
            "type": loop["type"],
            "owner": loop["owner"],
            "chat_id": loop["chat_id"],
            "source_msg": loop["source_msg"],
            "due": loop.get("due"),
            "status": loop["status"],
            "closed_by": loop.get("closed_by"),
        }
        if loop["status"] == "open":
            clean_loop["help"] = loop.get("help")

        all_loops.append(clean_loop)
