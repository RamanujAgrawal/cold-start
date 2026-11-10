"""Data loading and preprocessing utilities."""
import json
from pathlib import Path
from collections import defaultdict


def read_jsonl(path):
    """Read a JSONL file and return a list of dicts."""
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_input(input_dir):
    """Load all input data from a folder."""
    folder = Path(input_dir)
    chats = read_jsonl(folder / "chats.jsonl")
    messages = read_jsonl(folder / "messages.jsonl")
    seed = {}
    seed_path = folder / "seed.json"
    if seed_path.exists():
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
    return chats, messages, seed


def build_indices(chats, messages):
    """Build useful lookup structures from raw data."""
    # Messages grouped by chat
    msgs_by_chat = defaultdict(list)
    for m in messages:
        msgs_by_chat[m["chat_id"]].append(m)
    # Sort each chat's messages by timestamp
    for cid in msgs_by_chat:
        msgs_by_chat[cid].sort(key=lambda m: m["ts"])

    # Chat lookup
    chat_by_id = {c["chat_id"]: c for c in chats}

    # All person IDs (excluding ME)
    all_people = sorted({p for c in chats for p in c["participants"]} - {"ME"})

    # Which chats each person participates in
    chats_per_person = defaultdict(set)
    for c in chats:
        for p in c["participants"]:
            if p != "ME":
                chats_per_person[p].add(c["chat_id"])

    # Messages sent by each person (across all chats)
    msgs_by_person = defaultdict(list)
    for m in messages:
        if m["sender_id"] != "ME":
            msgs_by_person[m["sender_id"]].append(m)

    # Group vs 1:1 chats
    group_chats = [c for c in chats if c["is_group"]]
    dm_chats = [c for c in chats if not c["is_group"]]

    # Person names (take the most common sender_name for each person)
    name_counts = defaultdict(lambda: defaultdict(int))
    for m in messages:
        if m["sender_id"] != "ME" and m.get("sender_name"):
            name_counts[m["sender_id"]][m["sender_name"]] += 1
    person_names = {}
    for pid, names in name_counts.items():
        person_names[pid] = max(names, key=names.get)

    # ME's name (from messages where sender_id is ME)
    me_names = defaultdict(int)
    for m in messages:
        if m["sender_id"] == "ME" and m.get("sender_name"):
            me_names[m["sender_name"]] += 1
    me_name = max(me_names, key=me_names.get) if me_names else "User"

    return {
        "msgs_by_chat": dict(msgs_by_chat),
        "chat_by_id": chat_by_id,
        "all_people": all_people,
        "chats_per_person": dict(chats_per_person),
        "msgs_by_person": dict(msgs_by_person),
        "group_chats": group_chats,
        "dm_chats": dm_chats,
        "person_names": person_names,
        "me_name": me_name,
    }


def format_chat_summary(chat, msgs, max_msgs=50):
    """Format a chat's messages into a readable summary for LLM prompts."""
    lines = []
    lines.append(f"Chat: {chat['name']} ({'group' if chat['is_group'] else '1:1'})")
    lines.append(f"Participants: {', '.join(chat['participants'])}")
    lines.append("---")
    for m in msgs[:max_msgs]:
        sender = m.get("sender_name", m["sender_id"])
        body = (m.get("body") or "").strip()
        if not body or body == "[photo]":
            continue
        kind_tag = ""
        if m["kind"] != "text":
            kind_tag = f"[{m['kind']}] "
        lines.append(f"[{m['id']}] {sender}: {kind_tag}{body}")
    return "\n".join(lines)


def format_messages_window(msgs, person_names=None):
    """Format a window of messages for LLM consumption."""
    lines = []
    for m in msgs:
        sender = m.get("sender_name") or (person_names or {}).get(m["sender_id"], m["sender_id"])
        body = (m.get("body") or "").strip()
        if not body:
            continue
        lines.append(f"[{m['id']}] {sender}: {body}")
    return "\n".join(lines)


def chunk_messages(msgs, chunk_size=60, overlap=10):
    """Split a list of messages into overlapping chunks."""
    chunks = []
    for i in range(0, len(msgs), chunk_size - overlap):
        chunk = msgs[i:i + chunk_size]
        if chunk:
            chunks.append(chunk)
    return chunks
