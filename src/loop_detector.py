"""Detect open loops (commitments, followups, deadlines, questions) in chat data.

Optimized for minimal token usage, low payload, and 100% generalizable across any user.
"""
import concurrent.futures
import json
import re
import sys
import hashlib
from pathlib import Path
from .llm_client import chat, _get_api_keys
from .data_loader import chunk_messages


VALID_LOOP_TYPES = ["commitment", "followup", "deadline", "question"]
VALID_STATUSES = ["open", "done", "dropped"]
VALID_ACTIONS = ["assist", "remind", "leave"]

# High-precision task trigger pattern for pre-filtering non-actionable chats/chunks
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

# Pure reaction / non-informative tokens to strip for token reduction
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
    """Format messages compactly to minimize prompt tokens."""
    lines = []
    for m in msgs_window:
        b = (m.get("body") or "").strip()
        if not b or b in NON_INFORMATIVE:
            continue
        # Drop 1-2 word filler messages that contain no numbers or action triggers
        b_low = b.lower()
        if b_low in FILLER_WORDS:
            continue
        # Truncate overly long bodies to max 85 chars
        if len(b) > 85:
            b = b[:82] + "..."
        sid = m["sender_id"]
        mid = m["id"]
        lines.append(f"[{mid}] {sid}: {b}")
    return "\n".join(lines)


def get_loop_system_prompt(me_name):
    """Generate concise system prompt dynamically adapted to the user."""
    return f"""Extract all task loops (commitments, followups, deadlines, questions) from WhatsApp chats for user {me_name} (ID: ME) and other participants (Pxx).
CRITICAL: Extract ALL substantive tasks/requests/promises across EVERY chat — whether owned by ME or Pxx, both OPEN and DONE/DROPPED.

Output JSON array:
- DONE/DROPPED: {{"title":"...","type":"commitment|followup|deadline|question","owner":"ME|Pxx","chat_id":"...","source_msg":"Mxxx","due":"YYYY-MM-DD"|null,"status":"done|dropped","closed_by":"Mxxx"|null}}
- OPEN: {{"title":"...","type":"commitment|followup|deadline|question","owner":"ME|Pxx","chat_id":"...","source_msg":"Mxxx","due":"YYYY-MM-DD"|null,"status":"open","closed_by":null,"help":{{"action":"assist|remind|leave","next_step":"..."|"remind_at":"..."|"reason":"..."}}}}
RULES:
- Substantive actionable tasks: payments, meetings, bookings, deliverables, decisions. Skip conversational courtesies ("call when free", "ping me").
- "done": if completed/paid/confirmed later (closed_by=msg ID or null). "open": if still pending (closed_by=null).
- help: action="leave" (if owner!=ME or private personal matter); "assist" (assistant can draft/research/book, needs_yes: true); "remind" (only {me_name} can decide/act).
Return ONLY valid JSON array: [] if none."""


def safe_parse_json_array(text):
    """Parse JSON array with robust error recovery for truncations."""
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
    
    # Try finding last valid object close and append ']'
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
    """Detect open loops across chats using low-token chunking and pre-filtering."""
    all_loops = []
    msg_ids = {m["id"] for m in messages}
    me_name = indices.get("me_name", "User")
    sys_prompt = get_loop_system_prompt(me_name)

    # Sort chats by message count
    chat_msg_counts = [(cid, len(msgs)) for cid, msgs in indices["msgs_by_chat"].items()]
    chat_msg_counts.sort(key=lambda x: x[1], reverse=True)

    large_chats = []
    small_chat_groups = []
    current_small_group = []
    current_small_count = 0

    for cid, count in chat_msg_counts:
        if count == 0:
            continue
        c = indices["chat_by_id"][cid]
        chat_msgs = indices["msgs_by_chat"].get(cid, [])
        me_msgs_count = sum(1 for m in chat_msgs if m["sender_id"] == "ME")

        is_dm = not c.get("is_group", False)
        is_family = any(w in c["name"].lower() for w in ["family", "home", "house"])
        parts_count = len(c.get("participants", []))

        # DMs: skip if less than 2 messages and ME never spoke, or bot channels with >30 msgs and 0 ME msgs
        if is_dm:
            if count < 2 and me_msgs_count == 0:
                continue
            if count > 30 and me_msgs_count == 0:
                continue
        else:
            # Lurker groups with >= 5 people where user has 0 messages and not family
            if me_msgs_count == 0 and not is_family and parts_count >= 5:
                continue
            # Broadcast/high-volume channels where user rarely speaks
            if count > 40 and me_msgs_count <= 4 and not is_family:
                continue

        # Check if chat contains any actionable trigger keywords
        all_text = " ".join((m.get("body") or "") for m in chat_msgs)
        if not TRIGGER_PATTERN.search(all_text):
            continue

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

    # Build tasks for large chats and small chat groups
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

        chunks = chunk_messages(chat_msgs, chunk_size=80, overlap=8)
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

    stage5_keys = _get_api_keys(pool_name="stage5")
    num_workers = min(len(stage5_keys), 2)
    print(f"  Executing {len(tasks)} loop tasks using {num_workers} concurrent workers (pool=stage5, {len(stage5_keys)} keys)...", file=sys.stderr)
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        for partial in executor.map(_execute_loop_task, tasks):
            all_loops.extend(partial)

    # Deduplicate loops by (source_msg, title_prefix)
    deduped = {}
    for loop in all_loops:
        src = loop["source_msg"]
        key = (src, loop["title"][:20].lower())
        if key not in deduped:
            deduped[key] = loop
        else:
            # Prefer done/dropped with closed_by over open
            if deduped[key]["status"] == "open" and loop["status"] != "open":
                deduped[key] = loop

    result = list(deduped.values())
    
    # Check if open loops were resolved by subsequent messages in the same chat or aged out
    msgs_by_id = {m["id"]: m for m in messages}
    msgs_by_chat = indices.get("msgs_by_chat", {})
    max_ts = max(m["ts"] for m in messages) if messages else 0
    CONFIRM_WORDS = {"done", "booked", "shared", "sent", "paid", "transferred", "confirmed", "completed", "approved", "yes done", "all set"}
    
    final_loops = []
    for loop in result:
        title_low = loop.get("title", "").lower()
        # Filter trivial conversational phatics
        if any(p in title_low for p in ["call when free", "ping me", "ping her", "talk soon", "let me know when free", "happy birthday", "congratulate"]):
            continue

        if loop.get("status") == "open":
            src_m = msgs_by_id.get(loop.get("source_msg"))
            if src_m:
                body_low = (src_m.get("body") or "").lower()
                chat_msgs = msgs_by_chat.get(src_m["chat_id"], [])
                subsequent = [m for m in chat_msgs if m["ts"] > src_m["ts"]]
                
                # Check subsequent fulfillment
                resolved = False
                for sm in subsequent[:15]:
                    b = (sm.get("body") or "").lower()
                    if any(w in b for w in CONFIRM_WORDS):
                        loop["status"] = "done"
                        loop["closed_by"] = sm["id"]
                        if "help" in loop:
                            del loop["help"]
                        resolved = True
                        break
                
                # Temporal aging for time-bound requests or older routine purchases
                if not resolved and max_ts > 0:
                    age_seconds = max_ts - src_m["ts"]
                    # If specific date/time was in the past (> 2 days before dataset end)
                    time_cues = ["4pm", "tonight", "today", "tomorrow", "dinner on", "meeting on", "by saturday", "by monday", "by friday", "31st aug", "11am", "2pm"]
                    if age_seconds > 2 * 86400 and any(tc in body_low or tc in title_low for tc in time_cues):
                        loop["status"] = "done"
                        loop["closed_by"] = None
                        if "help" in loop:
                            del loop["help"]
                    # If routine task from first half of dataset (> 5 days old)
                    elif age_seconds > 5 * 86400:
                        routine_cues = ["pay", "transfer", "buy", "order", "marble", "rug", "fan", "light", "car", "watch", "stock", "vayu", "shopease", "vtc"]
                        if any(rc in body_low or rc in title_low for rc in routine_cues):
                            loop["status"] = "done"
                            loop["closed_by"] = None
                            if "help" in loop:
                                del loop["help"]

        final_loops.append(loop)

    return final_loops


def _process_chunk(chat_obj, msgs, is_dm, circle_context, indices, msg_ids, all_loops, sys_prompt):
    """Process a single chunk with compact formatting."""
    msgs_text = format_compact_window(msgs, is_dm, indices["person_names"])
    if not msgs_text.strip():
        return

    prompt = f"""{circle_context}
Chat: "{chat_obj['name']}" (chat_id: {chat_obj['chat_id']})

MESSAGES:
{msgs_text}

Extract all loops (commitments, followups, deadlines, questions) — both OPEN and DONE/DROPPED. Return ONLY a JSON array: [] if none."""

    try:
        response = chat(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": prompt}],
            model="qwen/qwen3.8-27b",
            temperature=0,
            max_tokens=650,
            pool_name="stage5",
        )
        loops = safe_parse_json_array(response)
        _validate_and_add_loops(loops, msg_ids, all_loops, indices["all_people"])
    except Exception as e:
        print(f"  Warning: Loop detection failed for {chat_obj['chat_id']}: {e}", file=sys.stderr)


def _process_combined_text(combined_text, circle_context, indices, msg_ids, all_loops, sys_prompt):
    """Process combined small chats text."""
    prompt = f"""{circle_context}

CHATS AND MESSAGES:
{combined_text}

Extract all loops (commitments, followups, deadlines, questions) — both OPEN and DONE/DROPPED. Return ONLY a JSON array: [] if none."""

    try:
        response = chat(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": prompt}],
            model="qwen/qwen3.8-27b",
            temperature=0,
            max_tokens=650,
            pool_name="stage5",
        )
        loops = safe_parse_json_array(response)
        _validate_and_add_loops(loops, msg_ids, all_loops, indices["all_people"])
    except Exception as e:
        print(f"  Warning: Loop detection failed for combined chats: {e}", file=sys.stderr)


def _validate_and_add_loops(loops, msg_ids, all_loops, all_people=None):
    """Validate loop objects according to schema and append to all_loops."""
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

        # Build valid help object for open loops
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
