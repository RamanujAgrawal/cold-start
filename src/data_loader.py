"""Data ingestion, indexing, and chat preprocessing utilities.

Loads raw WhatsApp exports (chats.jsonl, messages.jsonl, seed.json) and builds
in-memory lookup structures for fast relational traversals across the pipeline.
"""
import json
from pathlib import Path
from collections import defaultdict


def read_jsonl(path):
    """Read a JSONL file line-by-line into a list of dictionaries."""
    file_path = Path(path)
    if not file_path.exists():
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_input(input_dir):
    """Loads all starter assets from the target input directory."""
    folder = Path(input_dir)
    chats = read_jsonl(folder / "chats.jsonl")
    messages = read_jsonl(folder / "messages.jsonl")
    seed = {}
    seed_path = folder / "seed.json"
    if seed_path.exists():
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
    return chats, messages, seed


def build_indices(chats, messages):
    """Builds optimized in-memory lookup graphs from the raw dataset.

    Returns:
        A dictionary containing:
        - msgs_by_chat: Messages partitioned per chat_id and sorted chronologically.
        - chat_by_id: Direct lookup mapping chat_id -> chat dict.
        - all_people: Sorted list of all unique participant IDs (excluding 'ME').
        - chats_per_person: Set of chat IDs each person participates in.
        - msgs_by_person: List of messages authored by each person.
        - group_chats: Filtered list of group conversations.
        - dm_chats: Filtered list of 1:1 direct messages.
        - person_names: Most frequent sender_name per person ID.
        - me_name: Detected display name of the primary user ('ME').
    """
    # 1. Group and chronologically sort messages per chat
    msgs_by_chat = defaultdict(list)
    for m in messages:
        msgs_by_chat[m["chat_id"]].append(m)
    for cid in msgs_by_chat:
        msgs_by_chat[cid].sort(key=lambda m: m["ts"])

    # 2. Chat lookup dictionary
    chat_by_id = {c["chat_id"]: c for c in chats}

    # 3. Unique participant IDs across all chats (excluding 'ME')
    all_people = sorted({p for c in chats for p in c.get("participants", [])} - {"ME"})

    # 4. Map each person to the chats they participate in
    chats_per_person = defaultdict(set)
    for c in chats:
        for p in c.get("participants", []):
            if p != "ME":
                chats_per_person[p].add(c["chat_id"])

    # 5. Index messages authored by non-ME participants
    msgs_by_person = defaultdict(list)
    for m in messages:
        sid = m.get("sender_id")
        if sid and sid != "ME":
            msgs_by_person[sid].append(m)

    # 6. Separate 1:1 DMs from group chats
    group_chats = [c for c in chats if c.get("is_group")]
    dm_chats = [c for c in chats if not c.get("is_group")]

    # 7. Disambiguate contact names: in WhatsApp exports, names can fluctuate slightly.
    # We take the modal (most frequent) sender_name for each person ID.
    name_counts = defaultdict(lambda: defaultdict(int))
    for m in messages:
        sid = m.get("sender_id")
        sname = m.get("sender_name")
        if sid and sid != "ME" and sname:
            name_counts[sid][sname] += 1
    person_names = {}
    for pid, names in name_counts.items():
        person_names[pid] = max(names, key=names.get)

    # Fallback to chat names for 1:1 conversations if sender_name was absent
    for c in dm_chats:
        c_parts = [p for p in c.get("participants", []) if p != "ME"]
        if len(c_parts) == 1 and c_parts[0] not in person_names and c.get("name"):
            person_names[c_parts[0]] = c["name"]

    # 8. Extract user's display name ('ME') from messages
    me_names = defaultdict(int)
    for m in messages:
        if m.get("sender_id") == "ME" and m.get("sender_name"):
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
    """Formats a concise textual summary of a chat for LLM prompts."""
    lines = [
        f"Chat: {chat.get('name', '')} ({'group' if chat.get('is_group') else '1:1'})",
        f"Participants: {', '.join(chat.get('participants', []))}",
        "---"
    ]
    for m in msgs[:max_msgs]:
        sender = m.get("sender_name", m.get("sender_id", ""))
        body = (m.get("body") or "").strip()
        if not body or body == "[photo]":
            continue
        kind_tag = f"[{m['kind']}] " if m.get("kind") and m["kind"] != "text" else ""
        lines.append(f"[{m['id']}] {sender}: {kind_tag}{body}")
    return "\n".join(lines)


def format_messages_window(msgs, person_names=None):
    """Formats a sequential window of messages with sender attribution."""
    lines = []
    for m in msgs:
        sid = m.get("sender_id", "")
        sender = m.get("sender_name") or (person_names or {}).get(sid, sid)
        body = (m.get("body") or "").strip()
        if not body:
            continue
        lines.append(f"[{m['id']}] {sender}: {body}")
    return "\n".join(lines)


def chunk_messages(msgs, chunk_size=60, overlap=10):
    """Partitions a message sequence into overlapping sliding windows.
    
    Using a sliding window with overlap ensures commitments or context spanning
    across chunk boundaries are not fragmented or lost.
    """
    if not msgs:
        return []
    chunks = []
    step = max(1, chunk_size - overlap)
    for i in range(0, len(msgs), step):
        chunk = msgs[i:i + chunk_size]
        if chunk:
            chunks.append(chunk)
    return chunks
