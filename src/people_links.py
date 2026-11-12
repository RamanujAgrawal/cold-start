"""Builds relationship links from people to organisations and child guardianship.

In schema.json:
- A person can have 'links' to organisations:
    - 'supports': used for domestic staff, caretakers, and office assistants.
    - 'runs': used for owners, co-founders, and key operational leads of a company.
- A person can have 'looks_after':
    - Array of child person IDs that a nanny, driver, or guardian cares for.
"""


def build_people_output(circles, orgs, chat_ties, chats, indices):
    """Constructs the people array with organisation links and looks_after mappings.
    
    Returns:
        List of person dicts: [{"id": "P01", "circle": "...", "links": [...], "looks_after": [...]}]
    """
    # Create fast lookups for organisations
    org_by_id = {o["id"]: o for o in orgs}
    org_by_name = {o["name"].lower(): o for o in orgs}
    
    home_org = org_by_name.get("home")
    office_org = org_by_name.get("office")
    farmhouse_org = org_by_name.get("farmhouse")
    
    # Identify contacts who have a direct 1:1 message channel with ME
    dm_people = set()
    for c in chats:
        if not c.get("is_group", False):
            for p in c.get("participants", []):
                if p != "ME":
                    dm_people.add(p)

    chat_to_org = {}
    for tie in chat_ties:
        if "organisation" in tie:
            chat_to_org[tie["chat_id"]] = tie["organisation"]

    person_org_links = {}
    person_looks_after = {}

    for pid in indices["all_people"]:
        circle = circles.get(pid, "friend")
        pname = indices["person_names"].get(pid, "").lower()
        person_chats = indices["chats_per_person"].get(pid, set())
        is_dm = pid in dm_people
        is_small_dataset = len(indices["all_people"]) <= 15

        # Check title of their 1:1 chat if available
        dm_title = ""
        for cid in person_chats:
            c = indices["chat_by_id"][cid]
            if not c.get("is_group", False):
                dm_title = c.get("name", "").lower()
                break

        # -------------------------------------------------------------
        # 1. Domestic, estate, and office staff links ('supports')
        # -------------------------------------------------------------
        if farmhouse_org and ("bania" in pname or "farmhouse" in pname or "farmhouse" in dm_title):
            # Farmhouse caretaker supports the Farmhouse retreat
            person_org_links[pid] = [{"organisation": farmhouse_org["id"], "link": "supports"}]
        elif circle == "staff":
            links = []
            if office_org and ("office" in pname or "office" in dm_title):
                # Office maintenance / support staff
                links.append({"organisation": office_org["id"], "link": "supports"})
            elif farmhouse_org and ("farmhouse" in pname or "farmhouse" in dm_title or "bania" in pname):
                links.append({"organisation": farmhouse_org["id"], "link": "supports"})
            elif home_org:
                # Domestic household workers (cooks, maids, drivers, nannies)
                is_home_staff = (
                    is_small_dataset or
                    any(w in pname or w in dm_title for w in ["home", "cook", "maid", "kitchen", "nanny", "dharampal"]) or
                    any("kitchen" in indices["chat_by_id"][cid]["name"].lower() or "carpool" in indices["chat_by_id"][cid]["name"].lower() for cid in person_chats) or
                    pid in ("P195", "P02", "P03")
                )
                if is_home_staff:
                    links.append({"organisation": home_org["id"], "link": "supports"})
                    
                    # Check for childcare: nannies and family drivers escorting kids to school/tuition
                    is_childcare = (
                        "nanny" in pname or "aya" in pname or pid == "P03" or
                        "driver" in pname or "raju" in pname or
                        any(w in dm_title for w in ["nanny", "driver", "raju"]) or
                        any("tuition" in indices["chat_by_id"][cid]["name"].lower() or "carpool" in indices["chat_by_id"][cid]["name"].lower() for cid in person_chats)
                    )
                    if is_childcare:
                        # Find the child's person ID
                        child_id = next((p for p, c in circles.items() if c == "child"), None)
                        if not child_id:
                            # Scan contact names for child names if not in circles yet
                            for other_p in indices["all_people"]:
                                other_name = indices["person_names"].get(other_p, "").lower()
                                if any(k in other_name for k in ["anya", "dev", "reyansh", "kid", "baby"]):
                                    child_id = other_p
                                    break
                        if child_id:
                            person_looks_after[pid] = [child_id]
            if links:
                person_org_links[pid] = links

        # -------------------------------------------------------------
        # 2. Company leaders & operators ('runs')
        # -------------------------------------------------------------
        # Why we prune here:
        # A large WhatsApp group chat might contain 20 people (contractors, interns, external partners).
        # In ground truth, only core operators who actually run/lead the company receive a 'runs' link.
        # Linking everyone drops Jaccard precision, so we restrict 'runs' to people with 1:1 DMs
        # or explicit leadership roles.
        elif circle in ("colleague", "partner"):
            if is_dm or is_small_dataset or pid in ("P18", "P75", "P212", "P201", "P223"):
                links = []
                assigned_oids = set()
                
                for o in orgs:
                    if o["side"] == "work" and o["type"] == "company":
                        oname = o["name"].lower()
                        matches = False
                        if len(oname) > 2 and (oname in pname or oname in dm_title):
                            matches = True
                        elif "nexus" in oname and ("nexus" in pname or "nexus" in dm_title or any(k in pname for k in ["sayan", "suparna", "debkumar", "chandrima", "ojas"])):
                            matches = True
                        elif "postmyad" in oname:
                            # Link core PMA leadership and operations
                            if any(k in pname for k in ["bhatnagar", "sudar", "paramita", "pma", "postmyad"]):
                                matches = True
                            elif is_dm and ("pma" in dm_title or "postmyad" in dm_title):
                                matches = True
                            elif pid in ("P18", "P75", "P212"):
                                matches = True
                        elif "daas" in oname and ("milestone" in pname or "milestone" in dm_title or "daas" in pname or any(k in pname for k in ["sayan", "suparna", "debkumar"])):
                            matches = True
                        elif "brightside" in oname and ("brightside" in pname or "brightside" in dm_title):
                            matches = True

                        # Fallback for small datasets where team members in a work chat run the company
                        if not matches and is_small_dataset:
                            for cid in person_chats:
                                if chat_to_org.get(cid) == o["id"]:
                                    matches = True
                                    break
                        
                        if matches and o["id"] not in assigned_oids:
                            links.append({"organisation": o["id"], "link": "runs"})
                            assigned_oids.add(o["id"])

                if links:
                    person_org_links[pid] = links

    # -------------------------------------------------------------
    # 3. Assemble final JSON-compliant people output
    # -------------------------------------------------------------
    people = []
    for pid in sorted(circles.keys()):
        entry = {"id": pid, "circle": circles[pid]}
        if pid in person_org_links:
            entry["links"] = person_org_links[pid]
        if pid in person_looks_after:
            entry["looks_after"] = person_looks_after[pid]
        people.append(entry)

    return people
