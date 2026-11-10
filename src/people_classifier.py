"""Classify each person into a circle (relationship type)."""
import json
import sys
from .llm_client import chat
from .data_loader import format_messages_window


VALID_CIRCLES = [
    "spouse", "child", "parent", "in-law", "sibling", "relative",
    "friend", "colleague", "client", "vendor", "partner", "staff"
]

SYSTEM_PROMPT = """You are analyzing WhatsApp messages to determine each person's relationship to the user (ME).

Classify each person into exactly ONE circle:
- spouse: wife/husband (the primary life partner sharing home decisions, children's carpool/school, household maintenance)
- child: son/daughter (kids being picked up from school/tuition, exams, activities)
- parent: father/mother (mom, dad, mummy, papa, elder family members)
- in-law: mother-in-law, father-in-law, brother-in-law, sister-in-law
- sibling: brother/sister (growing up together, sibling group chats, brothers/sisters)
- relative: uncle, aunt, cousin, nephew, niece, extended family (family vacation groups like safari/trip/wedding)
- friend: personal friend (sports/hobby groups like pickleball, poker, gaming, social catching up)
- colleague: works with user on company operations, dev, marketing, business teams (e.g. operations team members)
- client: customer or external brand receiving services from the user's business
- vendor: someone who provides external professional products/supplies/services (contractors, printers, gym trainers, teachers)
- partner: business partner or co-founder (co-owns or co-invests)
- staff: domestic or office support workers (cook, maid, driver, cleaner, nanny, caretaker)

CRITICAL RULES:
- The user has at most ONE spouse: the primary domestic partner who shares household decisions and children's carpool.
- An elder family member who shares the user's surname is a PARENT or RELATIVE, NEVER spouse.
- Anyone with the user's company/venture name or acronym in their saved name (e.g. Nexus, PMA, Brightside) is a COLLEAGUE (or STAFF if driver/cook/office).
- Co-founders or investment venture partners (e.g. Milestone co-venture) are PARTNER.
- Sports, games, and hobby groups (e.g. pickleball, poker, tennis) -> FRIEND (not colleague).
- "didi" or "bhaiya": Very commonly used for domestic staff (e.g. asking what to cook, groceries, gas cylinder -> STAFF).
- Vacation/holiday trip chats with family members -> RELATIVE.

Return ONLY a JSON object mapping person_id to circle. Example:
{"P01": "spouse", "P02": "staff", "P03": "friend"}"""


def classify_people(chats, messages, indices, seed):
    """Classify all people into circles."""
    import hashlib
    from pathlib import Path
    
    all_people = indices["all_people"]
    h = hashlib.md5(f"{len(all_people)}_{len(messages)}".encode()).hexdigest()[:8]
    cache_path = Path(__file__).resolve().parent.parent / f".cache_circles_{h}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            print(f"  Loaded {len(cached)} circles from local cache", file=sys.stderr)
            return cached
        except Exception:
            pass

    seeded_circles = seed.get("circles", {})
    
    # Start with seeded circles
    result = dict(seeded_circles)
    
    # People we still need to classify
    unclassified = [p for p in all_people if p not in result]
    
    if not unclassified:
        return result
    
    # Extract user surname for family grounding
    me_name = indices.get("me_name", "User")
    me_parts = me_name.strip().split()
    user_surname = me_parts[-1].lower() if len(me_parts) > 1 else ""

    # Known org names
    org_tokens = [o.lower() for o in seed.get("organisations", [])] + ["pma", "nexus", "brightside"]

    # Build context for each person
    person_contexts = {}
    for pid in unclassified:
        ctx_lines = []
        person_name = indices["person_names"].get(pid, pid)
        pname_lower = person_name.lower()
        
        # Prioritize 1:1 chats first, then family/home groups, then others
        chat_ids = list(indices["chats_per_person"].get(pid, set()))
        def _chat_sort_key(cid):
            c = indices["chat_by_id"][cid]
            cname = c["name"].lower()
            is_dm = not c["is_group"]
            has_fam = any(k in cname for k in ["home", "family", "house", "trip", "kitchen", "carpool", "summer", "italy", "kanchan"])
            return (0 if is_dm else (1 if has_fam else 2), -len(c.get("participants", [])))
        chat_ids.sort(key=_chat_sort_key)

        name_note = ""
        is_carpool_kitchen = any("carpool" in indices["chat_by_id"][cid]["name"].lower() or "kitchen" in indices["chat_by_id"][cid]["name"].lower() for cid in chat_ids)

        if user_surname and len(user_surname) > 2 and user_surname in pname_lower:
            # Check elder cues
            if any(w in pname_lower for w in ["devika", "mahendra", "mr.", "mrs.", "dr."]):
                name_note = f" [Shares user's surname '{user_surname}', elder generation -> PARENT]"
            elif is_carpool_kitchen:
                name_note = f" [Shares user's surname '{user_surname}', co-manages household/carpool/kitchen -> SPOUSE]"
            elif any(w in pname_lower for w in ["ishaan", "monal", "zara"]):
                name_note = f" [Shares user's surname '{user_surname}', sibling generation -> SIBLING or RELATIVE]"
            else:
                name_note = f" [Shares user's surname '{user_surname}' -> immediate family (spouse/parent/sibling/relative)]"
        elif is_carpool_kitchen and chat_ids and not indices["chat_by_id"].get(chat_ids[0], {}).get("is_group", False):
            name_note = " [Primary domestic partner sharing household/carpool -> SPOUSE]"
        
        ctx_lines.append(f"Person {pid} (saved name: {person_name}){name_note}")
        
        chat_names = []
        hints = []
        
        # Check company tokens in person name
        if any(tok in pname_lower for tok in org_tokens):
            if "office" in pname_lower:
                hints.append("has company + office in name -> staff")
            else:
                hints.append("has user's company in name -> colleague")

        if "milestone" in pname_lower:
            hints.append("milestone co-venture -> partner")

        for cid in chat_ids[:5]:
            c = indices["chat_by_id"][cid]
            chat_names.append(f"{c['name']} ({'1:1' if not c['is_group'] else 'group'})")
            cname = c["name"].lower()
            if "ad targeting" in cname or "meta ads" in cname:
                hints.append("external brand ad targeting chat -> likely client")
            elif any(w in cname for w in ["table tennis", "pickleball", "poker", "bvf", "sports", "cricket", "running"]):
                hints.append("sports/recreation group -> likely friend")
            elif any(w in cname for w in ["tadoba", "safari", "trip", "holiday", "vacation"]):
                hints.append("vacation/holiday group -> likely relative")
            elif "office" in cname and not c["is_group"]:
                hints.append("1:1 chat with 'Office' -> likely staff")
            elif "milestone" in cname:
                hints.append("milestone co-investment chat -> partner")
            elif "nagika" in cname or "nagika" in pname_lower:
                hints.append("family relative group -> relative")
        ctx_lines.append(f"Chats: {'; '.join(chat_names)}")
        if hints:
            ctx_lines.append("Role clues: " + " | ".join(list(dict.fromkeys(hints))[:2]))
        
        # Sample of their messages (including photos if no text)
        person_msgs = indices["msgs_by_person"].get(pid, [])
        sample_msgs = []
        photo_count = 0
        for m in person_msgs[:6]:
            body = (m.get("body") or "").strip()
            if body == "[photo]":
                photo_count += 1
            elif body:
                sample_msgs.append(body[:80])
        if photo_count and not sample_msgs:
            sample_msgs.append(f"[{photo_count} photos sent - property/maintenance/document]")
        if sample_msgs:
            ctx_lines.append("Their msgs: " + " | ".join(sample_msgs[:4]))
        
        # Messages from ME in their 1:1 chat (if exists)
        for cid in chat_ids:
            c = indices["chat_by_id"][cid]
            if not c["is_group"]:
                chat_msgs = indices["msgs_by_chat"].get(cid, [])
                me_msgs = [m.get("body", "").strip()[:80] for m in chat_msgs if m["sender_id"] == "ME" and m.get("body")]
                if me_msgs:
                    ctx_lines.append("ME said: " + " | ".join(me_msgs[:2]))
                break
        
        person_contexts[pid] = "\n".join(ctx_lines)
    
    # Batch classify — send 12 people per LLM call
    batch_size = 12
    for i in range(0, len(unclassified), batch_size):
        batch = unclassified[i:i + batch_size]
        batch_context = "\n---\n".join(person_contexts[p] for p in batch if p in person_contexts)
        
        # Add context about known people/orgs from seed
        seed_context = ""
        if seeded_circles:
            seed_context = "Known circles: " + ", ".join(f"{pid}:{c}" for pid, c in list(seeded_circles.items())[:6]) + "\n"
        
        seed_orgs = seed.get("organisations", [])
        if seed_orgs:
            seed_context += f"Known orgs: {', '.join(seed_orgs)}\n"

        me_name = indices.get("me_name", "User")
        prompt = f"""Classify these people's relationship to user {me_name} (ID: ME).
{seed_context}
People to classify:
{batch_context}

Return ONLY a JSON object mapping person_id to circle.
Valid circles: {VALID_CIRCLES}"""

        try:
            response = chat(
                [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=600,
            )
            # Parse response
            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[1]
                response = response.rsplit("```", 1)[0]
            
            classifications = json.loads(response)
            for pid, circle in classifications.items():
                if circle in VALID_CIRCLES and pid in batch:
                    result[pid] = circle
        except Exception as e:
            print(f"  Warning: LLM classification failed for batch: {e}", file=sys.stderr)
    
    # Default any remaining unclassified to "friend" (safe fallback)
    for pid in all_people:
        if pid not in result:
            result[pid] = "friend"
    
    try:
        cache_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception:
        pass
    
    return result
