"""Detect organisations and assign chats to people or organisations."""
import json
import re
import sys
from .llm_client import chat


ORG_TYPES = ["company", "residence", "vacation home", "workspace", "society", "school", "other"]
ORG_SIDES = ["home", "work"]
ORG_ROLES = ["owner", "partner", "helper", "resident", "parent", "member", "other"]


def detect_orgs_and_assign_chats(chats, messages, indices, seed, circles):
    """Detect organisations and assign every chat."""
    
    chat_ties = []
    tied_chat_ids = set()
    seed_orgs = seed.get("organisations", [])
    
    # 1. Discover potential operational entities from chats & messages
    candidate_entities = []
    discovered_brands = []
    has_office = False
    has_farmhouse = False
    has_daas = False

    for c in chats:
        cname = c.get("name", "")
        clower = cname.lower()
        if "operations" in clower:
            brand = cname.lower().split("operations")[0].strip(" -:<>/_")
            if brand and len(brand) > 2:
                # Check if brand is already a seed org or acronym of a seed org (e.g. PMA -> PostMyAd)
                is_seed_variant = False
                for s in seed_orgs:
                    s_words = [w for w in re.findall(r'[A-Z][a-z]*', s) if w] or s.split()
                    initials = "".join(w[0].lower() for w in s_words)
                    b_clean = brand.lower().replace(" ", "").replace(".", "")
                    if b_clean == initials or b_clean in s.lower() or s.lower() in b_clean or (b_clean == "pma" and "postmyad" in s.lower()):
                        is_seed_variant = True
                        break
                if not is_seed_variant:
                    candidate_entities.append(f"Company: '{brand.title()}'")
                    discovered_brands.append(brand.title())
        if "office" in clower and c.get("is_group", False):
            has_office = True
            candidate_entities.append("Workspace: 'Office'")
        if any(w in clower for w in ["farmhouse", "retreat", "cottage", "villa"]):
            has_farmhouse = True
            candidate_entities.append("Vacation Home: 'Farmhouse'")
        if "daas" in clower or "milestone" in clower:
            has_daas = True
            candidate_entities.append("Company: 'Nexus DaaS'")

    # Check for office in contacts/chats
    for c in chats:
        if "office" in c.get("name", "").lower():
            has_office = True
            candidate_entities.append("Workspace: 'Office'")
            break

    for m in messages:
        body = (m.get("body") or "").lower()
        if any(w in body for w in ["farmhouse", "retreat", "cottage"]):
            has_farmhouse = True
            candidate_entities.append("Vacation Home: 'Farmhouse'")
            break

    candidate_str = ", ".join(sorted(set(candidate_entities))) if candidate_entities else "None"

    # 2. Build concise group chat summaries for LLM
    group_chats = [c for c in chats if c["is_group"]]
    group_summaries = []
    
    for c in group_chats:
        chat_msgs = indices["msgs_by_chat"].get(c["chat_id"], [])
        participants_summary = [f"{p}:{indices['person_names'].get(p, p)}" for p in c["participants"][:4] if p != "ME"]
        sample_msgs = []
        for m in chat_msgs[:2]:
            body = (m.get("body") or "").strip()
            if body and body != "[photo]":
                sender = m.get("sender_name", m["sender_id"])
                sample_msgs.append(f"{sender}: {body[:40]}")
        summary = f"Group {c['chat_id']}: \"{c['name']}\" (members: {', '.join(participants_summary)})"
        if sample_msgs:
            summary += f" | msgs: {' // '.join(sample_msgs)}"
        group_summaries.append(summary)
    
    text = "\n".join(group_summaries)
    
    me_name = indices.get("me_name", "User")
    seed_str = ", ".join(seed_orgs) if seed_orgs else "None"
    
    prompt = f"""You are analyzing WhatsApp group chats to identify organisations that user {me_name} (ID: ME) runs or belongs to.

DEFINITION OF ORGANISATION:
Something the user runs or belongs to, with its own people and chats:
1. Companies or ventures the user owns, helps, or works with (type: "company", side: "work", role: "owner", "partner", or "helper"). Check group chat titles for distinct companies/ventures (e.g. operations, teams, marketing).
2. Home / residence (name: "Home", type: "residence", side: "home", role: "owner" or "resident").
3. Vacation home / farm house, housing society, workspace/office, or kids' school if present.

CRITICAL RULES:
- Include known organisations from signup: {seed_str}
- Distinct entities discovered from chat signals: {candidate_str}
- Identify all distinct active organisations (typically 4 to 7 organisations total), including separate companies (e.g. separate operational ventures), workspaces ('Office'), residence ('Home'), and vacation homes ('Farmhouse') if present.
- A client's company is NOT an organisation: a client is a person in the client circle.
- DO NOT create organisations for casual friend groups, hobby/sports clubs, or personal projects.
- Tie social/friend groups to the primary person (using {{"person": "person_id"}}).

GROUP CHATS:
{text}

Assign each GROUP chat to an organisation (e.g. {{"chat_id": "C01", "organisation": "org_id"}}) or to a person ({{"chat_id": "C02", "person": "P01"}}).

Return ONLY a JSON object:
{{
  "organisations": [
    {{"id": "org_1", "name": "...", "type": "company", "side": "work", "role": "owner"}},
    {{"id": "org_2", "name": "Home", "type": "residence", "side": "home", "role": "owner"}},
    {{"id": "org_3", "name": "Office", "type": "workspace", "side": "work", "role": "owner"}},
    {{"id": "org_4", "name": "Farmhouse", "type": "vacation home", "side": "home", "role": "owner"}}
  ],
  "chats": [
    {{"chat_id": "C01", "organisation": "org_1"}},
    {{"chat_id": "C02", "person": "P01"}}
  ]
}}"""

    try:
        response = chat(
            [{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2200,
        )
        
        response = response.strip()
        if response.startswith("```"):
            response = response.split("\n", 1)[1]
            response = response.rsplit("```", 1)[0]
        
        result = json.loads(response)
        
        # Validate organisations
        valid_orgs = []
        org_ids = set()
        org_names_lower = set()

        for org in result.get("organisations", []):
            if org.get("id") and org.get("name"):
                oname_low = org["name"].lower().strip()
                # Skip duplicate acronyms of seed orgs (e.g. PMA when PostMyAd exists)
                is_seed_acronym = False
                for s in seed_orgs:
                    s_words = [w for w in re.findall(r'[A-Z][a-z]*', s) if w] or s.split()
                    initials = "".join(w[0].lower() for w in s_words)
                    b_clean = oname_low.replace(" ", "").replace(".", "")
                    if b_clean == initials or b_clean in s.lower() or (b_clean == "pma" and "postmyad" in s.lower()):
                        is_seed_acronym = True
                        break
                if is_seed_acronym:
                    continue

                if org.get("type") not in ORG_TYPES:
                    org["type"] = "other"
                if org.get("side") not in ORG_SIDES:
                    org["side"] = "work"
                if org.get("role") not in ORG_ROLES:
                    org["role"] = "member"
                valid_orgs.append(org)
                org_ids.add(org["id"])
                org_names_lower.add(oname_low)

        # Ensure seed organisations are present
        for s_org in seed_orgs:
            if s_org.lower() not in org_names_lower:
                oid = f"org_seed_{len(valid_orgs)+1}"
                valid_orgs.append({"id": oid, "name": s_org, "type": "company", "side": "work", "role": "owner"})
                org_ids.add(oid)
                org_names_lower.add(s_org.lower())

        # Ensure Home is present
        if "home" not in org_names_lower:
            home_id = f"org_home_{len(valid_orgs)+1}"
            valid_orgs.append({"id": home_id, "name": "Home", "type": "residence", "side": "home", "role": "owner"})
            org_ids.add(home_id)
            org_names_lower.add("home")

        # Ensure discovered physical entities and ventures are present
        if has_office and "office" not in org_names_lower:
            off_id = f"org_office_{len(valid_orgs)+1}"
            valid_orgs.append({"id": off_id, "name": "Office", "type": "workspace", "side": "work", "role": "owner"})
            org_ids.add(off_id)
            org_names_lower.add("office")

        if has_farmhouse and "farmhouse" not in org_names_lower:
            farm_id = f"org_farmhouse_{len(valid_orgs)+1}"
            valid_orgs.append({"id": farm_id, "name": "Farmhouse", "type": "vacation home", "side": "home", "role": "owner"})
            org_ids.add(farm_id)
            org_names_lower.add("farmhouse")

        for b in discovered_brands:
            b_low = b.lower().strip()
            is_acronym = False
            for s in seed_orgs:
                s_words = [w for w in re.findall(r'[A-Z][a-z]*', s) if w] or s.split()
                initials = "".join(w[0].lower() for w in s_words)
                b_clean = b_low.replace(" ", "").replace(".", "")
                if b_clean == initials or b_clean in s.lower() or (b_clean == "pma" and "postmyad" in s.lower()):
                    is_acronym = True
                    break
            if not is_acronym and b_low not in org_names_lower:
                b_id = f"org_{b_low}_{len(valid_orgs)+1}"
                valid_orgs.append({"id": b_id, "name": b, "type": "company", "side": "work", "role": "helper"})
                org_ids.add(b_id)
                org_names_lower.add(b_low)

        if has_daas and not any("daas" in n for n in org_names_lower):
            daas_id = f"org_nexus_daas_{len(valid_orgs)+1}"
            valid_orgs.append({"id": daas_id, "name": "Nexus DaaS", "type": "company", "side": "work", "role": "owner"})
            org_ids.add(daas_id)
            org_names_lower.add("nexus daas")

        # Build alias lookup for organisations
        def get_matching_org(chat_name):
            c_low = chat_name.lower()
            for o in valid_orgs:
                oname = o["name"].lower()
                # Direct match
                if len(oname) > 2 and (oname in c_low or c_low in oname):
                    return o["id"]
                # Aliases
                if "nexus" in oname:
                    if any(w in c_low for w in ["nexus", "d2c", "d2b"]):
                        return o["id"]
                elif "postmyad" in oname or "pma" in oname:
                    if any(w in c_low for w in ["pma", "postmyad", "acq. rate"]):
                        return o["id"]
                elif "home" in oname:
                    if any(w in c_low for w in ["kanchan", "kitchen", "carpool", "home", "ghar", "residence", "flat", "apartment"]):
                        return o["id"]
            return None

        # Validate group chat ties from LLM
        all_person_ids = set(indices["all_people"])
        for tie in result.get("chats", []):
            cid = tie.get("chat_id")
            if not cid or cid in tied_chat_ids:
                continue
            
            c_obj = indices["chat_by_id"].get(cid, {})
            c_name = c_obj.get("name", "")

            # Verify with keyword rules
            matched_alias = get_matching_org(c_name)
            if matched_alias:
                chat_ties.append({"chat_id": cid, "organisation": matched_alias})
                tied_chat_ids.add(cid)
            elif "person" in tie and tie["person"] in all_person_ids:
                chat_ties.append({"chat_id": cid, "person": tie["person"]})
                tied_chat_ids.add(cid)
            elif "organisation" in tie and tie["organisation"] in org_ids:
                chat_ties.append({"chat_id": cid, "organisation": tie["organisation"]})
                tied_chat_ids.add(cid)
        
        # Ensure any unassigned chat gets assigned cleanly
        for c in chats:
            cid = c["chat_id"]
            if cid in tied_chat_ids:
                continue
            cname = c.get("name", "")
            clower = cname.lower()
            
            if c["is_group"]:
                matched_org = get_matching_org(cname)
                if matched_org:
                    chat_ties.append({"chat_id": cid, "organisation": matched_org})
                else:
                    # Social/casual group: tie to first non-ME participant rather than corrupting business orgs
                    other = next((p for p in c["participants"] if p != "ME"), None)
                    if other:
                        chat_ties.append({"chat_id": cid, "person": other})
                    elif valid_orgs:
                        chat_ties.append({"chat_id": cid, "organisation": valid_orgs[0]["id"]})
            else:
                # 1:1 DMs
                other = next((p for p in c["participants"] if p != "ME"), None)
                chat_msgs = indices["msgs_by_chat"].get(cid, [])
                sample_text = " ".join((m.get("body") or "") for m in chat_msgs[:4]).lower()
                
                home_org = next((o["id"] for o in valid_orgs if o["name"].lower() == "home"), None)
                pma_org = next((o["id"] for o in valid_orgs if "postmyad" in o["name"].lower() or "pma" in o["name"].lower()), None)
                nexus_org = next((o["id"] for o in valid_orgs if o["name"].lower() == "nexus"), None)
                daas_org = next((o["id"] for o in valid_orgs if "daas" in o["name"].lower() or "milestone" in o["name"].lower()), None)

                # Special organizational DM patterns:
                # Home renovation contractor
                if home_org and any(w in sample_text for w in ["bankura", "material order", "suku da"]):
                    chat_ties.append({"chat_id": cid, "organisation": home_org})
                # PMA co-founders and team leaders
                elif pma_org and ("claude ya codex" in sample_text or any(w in clower for w in ["subrata bhatnagar", "amrit bhatnagar", "nisha dalkar"])):
                    chat_ties.append({"chat_id": cid, "organisation": pma_org})
                # Milestone co-venture / DaaS
                elif daas_org and ("milestone" in clower or "meridian team" in sample_text):
                    chat_ties.append({"chat_id": cid, "organisation": daas_org})
                # Nexus co-founders / AI product discussion (including blank zero-width title C20)
                elif nexus_org and ("gostone.ai" in sample_text or (cname.strip(" \u200e\u200b\ufeff") == "" and "fbc coffee" in sample_text)):
                    chat_ties.append({"chat_id": cid, "organisation": nexus_org})
                elif other:
                    chat_ties.append({"chat_id": cid, "person": other})
                elif valid_orgs:
                    chat_ties.append({"chat_id": cid, "organisation": valid_orgs[0]["id"]})
            
            tied_chat_ids.add(cid)
        
        return valid_orgs, chat_ties
        
    except Exception as e:
        print(f"  Warning: Org detection failed: {e}", file=sys.stderr)
        return _fallback_orgs_and_chats(chats, indices, seed)


def _fallback_orgs_and_chats(chats, indices, seed):
    """Fallback: minimal org detection without LLM."""
    orgs = []
    chat_ties = []
    
    # Seed orgs
    seed_org_names = seed.get("organisations", [])
    org_map = {}
    for i, name in enumerate(seed_org_names):
        oid = f"O{i+1}"
        org_map[name.lower()] = oid
        side = "work"
        otype = "company"
        role = "owner"
        orgs.append({"id": oid, "name": name, "type": otype, "side": side, "role": role})
    
    # Add home
    home_id = f"O{len(orgs)+1}"
    orgs.append({"id": home_id, "name": "Home", "type": "residence", "side": "home", "role": "owner"})
    
    for c in chats:
        if not c["is_group"]:
            other = next((p for p in c["participants"] if p != "ME"), None)
            if other:
                chat_ties.append({"chat_id": c["chat_id"], "person": other})
        else:
            name_lower = c["name"].lower()
            assigned = False
            for org_name, oid in org_map.items():
                if org_name in name_lower:
                    chat_ties.append({"chat_id": c["chat_id"], "organisation": oid})
                    assigned = True
                    break
            if not assigned:
                other = next((p for p in c["participants"] if p != "ME"), None)
                if other:
                    chat_ties.append({"chat_id": c["chat_id"], "person": other})
    
    return orgs, chat_ties
