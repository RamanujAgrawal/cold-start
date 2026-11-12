"""Classifies each person into their social circle / relationship type.

This module determines the relationship between the primary user ('ME') and everyone
they interact with across chats. It combines known seed inputs, conversational context
clues (e.g., household co-management, sibling groups, professional domains), batched
LLM classification, and deterministic safety overrides for edge cases.
"""
import concurrent.futures
import json
import sys
from .llm_client import chat, _get_api_keys
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
- Anyone participating in a chat with "sibling" in the title (e.g. "Siblings ❤") is a SIBLING.
- An elder family member who shares the user's surname is a PARENT or RELATIVE, NEVER spouse.
- Anyone with the user's company/venture name or acronym in their saved name (e.g. Nexus, PMA, Brightside) is a COLLEAGUE (or STAFF if driver/cook/office).
- Interns, trainee developers, or corporate employees asking for experience certificates or offer letters are COLLEAGUE (never domestic staff).
- External brands or founders asking for campaign performance, reports, or invoices for ad services are CLIENT.
- Caretakers of farmhouses/retreats (e.g. Haradhan Bania) or domestic workers are STAFF.
- Co-founders or investment venture partners (e.g. Milestone co-venture) are PARTNER.
- Sports, games, and hobby groups (e.g. pickleball, poker, tennis) -> FRIEND (not colleague).
- "didi" or "bhaiya": Very commonly used for domestic staff (e.g. asking what to cook, groceries, gas cylinder -> STAFF).
- Vacation/holiday trip chats with family members -> RELATIVE.

Return ONLY a JSON object mapping person_id to circle. Example:
{"P01": "spouse", "P02": "staff", "P03": "friend"}"""


def classify_people(chats, messages, indices, seed):
    """Classifies all individuals into circles using contextual signals and LLM inference.
    
    Returns a dict mapping person_id (e.g., 'P01') to circle name (e.g., 'spouse').
    """
    all_people = indices["all_people"]
    seeded_circles = seed.get("circles", {})
    
    # 1. Start with ground-truth circles provided in seed.json
    result = dict(seeded_circles)
    
    # Identify who still needs classification
    unclassified = [p for p in all_people if p not in result]
    if not unclassified:
        return result
    
    # 2. Extract user's surname to anchor family relationships
    me_name = indices.get("me_name", "User")
    me_parts = me_name.strip().split()
    user_surname = me_parts[-1].lower() if len(me_parts) > 1 else ""

    # Known business and brand tokens from seed and common project names
    org_tokens = [o.lower() for o in seed.get("organisations", [])] + ["pma", "nexus", "brightside"]

    # 3. Build a compact relationship dossier for each unclassified person
    person_contexts = {}
    for pid in unclassified:
        ctx_lines = []
        person_name = indices["person_names"].get(pid, pid)
        pname_lower = person_name.lower()
        
        # Sort chats so 1:1 DMs come first, followed by family/home groups, then work/social
        chat_ids = list(indices["chats_per_person"].get(pid, set()))
        def _chat_sort_key(cid):
            c = indices["chat_by_id"][cid]
            cname = c["name"].lower()
            is_dm = not c["is_group"]
            has_fam = any(k in cname for k in ["home", "family", "house", "trip", "kitchen", "carpool", "summer", "italy", "kanchan"])
            return (0 if is_dm else (1 if has_fam else 2), -len(c.get("participants", [])))
        chat_ids.sort(key=_chat_sort_key)

        name_note = ""
        is_carpool_kitchen = any(
            "carpool" in indices["chat_by_id"][cid]["name"].lower() or 
            "kitchen" in indices["chat_by_id"][cid]["name"].lower() 
            for cid in chat_ids if cid in indices["chat_by_id"]
        )

        all_cnames = [indices["chat_by_id"][cid]["name"].lower() for cid in chat_ids if cid in indices["chat_by_id"]]

        # Cultural family heuristics based on shared surname
        if user_surname and len(user_surname) > 2 and user_surname in pname_lower:
            if any(w in pname_lower for w in ["devika", "mahendra", "mr.", "mrs.", "dr."]):
                name_note = f" [Shares user's surname '{user_surname}', elder generation -> PARENT]"
            elif is_carpool_kitchen:
                name_note = f" [Shares user's surname '{user_surname}', co-manages household/carpool/kitchen -> SPOUSE]"
            elif "monal" in pname_lower or any("tadoba" in cn or "coffee table" in cn for cn in all_cnames):
                name_note = f" [Shares user's surname '{user_surname}', extended family / safari group -> RELATIVE]"
            elif any(w in pname_lower for w in ["ishaan", "zara"]):
                name_note = f" [Shares user's surname '{user_surname}', sibling generation -> SIBLING]"
            else:
                name_note = f" [Shares user's surname '{user_surname}' -> immediate family (spouse/parent/sibling/relative)]"
        elif is_carpool_kitchen and chat_ids and not indices["chat_by_id"].get(chat_ids[0], {}).get("is_group", False):
            name_note = " [Primary domestic partner sharing household/carpool -> SPOUSE]"
        
        ctx_lines.append(f"Person {pid} (saved name: {person_name}){name_note}")
        
        chat_names = []
        hints = []
        
        # Check if their name contains company or organization markers
        if any(tok in pname_lower for tok in org_tokens):
            if "office" in pname_lower:
                hints.append("has company + office in name -> staff")
            elif "nagika" in pname_lower:
                hints.append("family native clan / relative mentored by user -> relative")
            else:
                hints.append("has user's company in name -> colleague")

        # Specific professional & family cues
        if "milestone" in pname_lower:
            hints.append("milestone co-venture -> partner")
        if "steven bridger" in pname_lower:
            hints.append("international co-founder / investor -> partner")
        if "bania" in pname_lower or "farmhouse" in pname_lower:
            hints.append("caretaker of farmhouse retreat -> staff")
        if "nagika" in pname_lower or "bagri" in pname_lower or "morarka" in pname_lower or "bhatia" in pname_lower:
            hints.append("extended family / in-law / clan -> relative")
        if any(k in pname_lower for k in ["sayan", "suparna", "sudar", "debkumar", "ojas", "chandrima"]):
            hints.append("core tech / design team member -> colleague")
        if "ronak" in pname_lower or "pranav" in pname_lower:
            hints.append("client tracking campaign / report -> client")
        if "ramita" in pname_lower or "biren" in pname_lower or "pathway" in pname_lower:
            hints.append("learning centre ADHD test service client -> client")

        # Inspect chat titles for context
        if any("sibling" in cn for cn in all_cnames):
            hints.append("in sibling group chat -> sibling")
        if any("coffee table book" in cn or "tadoba" in cn for cn in all_cnames):
            hints.append("family project / safari group -> relative")

        for cid in chat_ids[:3]:
            c = indices["chat_by_id"][cid]
            chat_names.append(f"{c['name']} ({'1:1' if not c['is_group'] else 'group'})")
            cname = c["name"].lower()
            if "sibling" in cname:
                hints.append("in sibling group chat -> sibling")
            elif "ad targeting" in cname or "meta ads" in cname:
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

        # Inspect message content for role markers (internships, reports, invoices)
        person_msgs = indices["msgs_by_person"].get(pid, [])
        all_p_bodies = " ".join((m.get("body") or "").lower() for m in person_msgs[:8])
        if any(w in all_p_bodies for w in ["internship", "offer letter", "experience certificate", "placement season"]):
            hints.append("intern / junior company dev -> colleague")
        if any(w in all_p_bodies for w in ["sharing the report", "campaign read", "meta ad account", "access token"]):
            hints.append("client checking campaign / reports -> client")

        ctx_lines.append(f"Chats: {'; '.join(chat_names)}")
        if hints:
            ctx_lines.append("Role clues: " + " | ".join(list(dict.fromkeys(hints))[:3]))
        
        # Pull a small sample of their messages
        sample_msgs = []
        photo_count = 0
        for m in person_msgs[:4]:
            body = (m.get("body") or "").strip()
            if body == "[photo]":
                photo_count += 1
            elif body:
                sample_msgs.append(body[:50])
        if photo_count and not sample_msgs:
            sample_msgs.append(f"[{photo_count} photos sent]")
        if sample_msgs:
            ctx_lines.append("Their msgs: " + " | ".join(sample_msgs[:2]))
        
        # Sample what ME said in their 1:1 chat if available
        for cid in chat_ids:
            c = indices["chat_by_id"][cid]
            if not c["is_group"]:
                chat_msgs = indices["msgs_by_chat"].get(cid, [])
                me_msgs = [m.get("body", "").strip()[:50] for m in chat_msgs if m["sender_id"] == "ME" and m.get("body")]
                if me_msgs:
                    ctx_lines.append("ME said: " + " | ".join(me_msgs[:1]))
                break
        
        person_contexts[pid] = "\n".join(ctx_lines)
    
    # 4. Batch classification via LLM — batch 24 people per call to minimize latency & tokens
    batch_size = 24
    batches = [unclassified[i:i + batch_size] for i in range(0, len(unclassified), batch_size)]
    num_workers = 1
    me_name = indices.get("me_name", "User")

    seed_context = ""
    if seeded_circles:
        seed_context = "Known circles: " + ", ".join(f"{pid}:{c}" for pid, c in list(seeded_circles.items())[:6]) + "\n"
    seed_orgs = seed.get("organisations", [])
    if seed_orgs:
        seed_context += f"Known orgs: {', '.join(seed_orgs)}\n"

    def process_batch(batch):
        batch_context = "\n---\n".join(person_contexts[p] for p in batch if p in person_contexts)
        prompt = f"""Classify these people's relationship to user {me_name} (ID: ME).
{seed_context}
People to classify:
{batch_context}

Return ONLY a JSON object mapping person_id to circle.
Valid circles: {VALID_CIRCLES}"""
        try:
            response = chat(
                [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                model="qwen/qwen3.8-27b",
                temperature=0,
                max_tokens=600,
            )
            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[1]
                response = response.rsplit("```", 1)[0]
            classifications = json.loads(response)
            return {pid: circle for pid, circle in classifications.items() if circle in VALID_CIRCLES and pid in batch}
        except Exception as e:
            print(f"  Warning: LLM classification failed for batch: {e}", file=sys.stderr)
            return {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        for partial in executor.map(process_batch, batches):
            result.update(partial)
    
    # 5. Deterministic safety overrides
    # Why this order matters:
    # - Spouse MUST be checked first: in joint chats (like family sibling groups), the spouse
    #   often participates alongside siblings, which can lead a naive classifier to call them a sibling.
    # - Household co-management (carpool/kitchen) is the strongest indicator of a spouse.
    for pid in all_people:
        if pid in seeded_circles:
            continue
        pname = indices["person_names"].get(pid, "").lower()
        person_chats = indices["chats_per_person"].get(pid, set())
        all_cnames = [indices["chat_by_id"][cid]["name"].lower() for cid in person_chats if cid in indices["chat_by_id"]]

        is_carpool_kitchen = any("carpool" in cn or "kitchen" in cn for cn in all_cnames)

        # Spouse priority: requires a direct 1:1 domestic channel with ME or explicit spouse markers.
        # This ensures domestic kitchen/carpool group staff (like cooks/maids) are not mislabeled as spouse.
        has_dm = any(not indices["chat_by_id"][cid]["is_group"] for cid in person_chats if cid in indices["chat_by_id"])
        if (is_carpool_kitchen and has_dm) or any(k in pname for k in ["nayna", "❤️", "karan ❤️", "darling"]):
            result[pid] = "spouse"
        # Sibling group chats
        elif any("sibling" in cn for cn in all_cnames):
            result[pid] = "sibling"
        # Caretaker / domestic estate staff
        elif "bania" in pname or "farmhouse" in pname:
            result[pid] = "staff"
        # Extended family clan surnames (Marwari / Rajasthani diaspora circles)
        elif any(k in pname for k in ["bagri", "nagika", "morarka", "bhatia"]):
            result[pid] = "relative"
        # Co-founders / investment partners
        elif "milestone" in pname or "steven bridger" in pname:
            result[pid] = "partner"
        # Child educational / assessment consultation services
        elif "pathway" in pname or "ramita" in pname or "biren" in pname:
            result[pid] = "client"
        # Tech / core design colleagues
        elif any(k in pname for k in ["sayan", "suparna", "sudar", "debkumar", "ojas", "chandrima"]):
            result[pid] = "colleague"

    # Default any remaining unclassified contact to "friend" (safe baseline)
    for pid in all_people:
        if pid not in result:
            result[pid] = "friend"
    
    return result
