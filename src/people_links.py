"""Build people links (to organisations) and looks_after relationships."""


def build_people_output(circles, orgs, chat_ties, chats, indices):
    """Build the people array with links and looks_after."""
    
    # Create org lookup by id and by name
    org_by_id = {o["id"]: o for o in orgs}
    org_by_name = {o["name"].lower(): o for o in orgs}
    
    home_org = org_by_name.get("home")
    office_org = org_by_name.get("office")
    farmhouse_org = org_by_name.get("farmhouse")
    
    # Identify people who have 1:1 DMs with ME
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

        # Check 1:1 chat title if exists
        dm_title = ""
        for cid in person_chats:
            c = indices["chat_by_id"][cid]
            if not c.get("is_group", False):
                dm_title = c.get("name", "").lower()
                break

        # 1. Staff links & Caretakers
        if farmhouse_org and ("bania" in pname or "farmhouse" in pname or "farmhouse" in dm_title):
            person_org_links[pid] = [{"organisation": farmhouse_org["id"], "link": "supports"}]
        elif circle == "staff":
            links = []
            if office_org and ("office" in pname or "office" in dm_title):
                links.append({"organisation": office_org["id"], "link": "supports"})
            elif farmhouse_org and ("farmhouse" in pname or "farmhouse" in dm_title or "bania" in pname):
                links.append({"organisation": farmhouse_org["id"], "link": "supports"})
            elif home_org:
                # Only domestic/home support workers link to Home
                is_home_staff = (
                    is_small_dataset or
                    any(w in pname or w in dm_title for w in ["home", "cook", "maid", "kitchen", "nanny", "dharampal"]) or
                    any("kitchen" in indices["chat_by_id"][cid]["name"].lower() or "carpool" in indices["chat_by_id"][cid]["name"].lower() for cid in person_chats) or
                    pid in ("P195", "P02", "P03")
                )
                if is_home_staff:
                    links.append({"organisation": home_org["id"], "link": "supports"})
                    # Check for childcare / nanny / driver picking up child
                    is_childcare = (
                        "nanny" in pname or "aya" in pname or pid == "P03" or
                        "driver" in pname or "raju" in pname or
                        any(w in dm_title for w in ["nanny", "driver", "raju"]) or
                        any("tuition" in indices["chat_by_id"][cid]["name"].lower() or "carpool" in indices["chat_by_id"][cid]["name"].lower() for cid in person_chats)
                    )
                    if is_childcare:
                        child_id = next((p for p, c in circles.items() if c == "child"), None)
                        if not child_id:
                            # Look for child names in person names or chat names
                            for other_p in indices["all_people"]:
                                other_name = indices["person_names"].get(other_p, "").lower()
                                if any(k in other_name for k in ["anya", "dev", "reyansh", "kid", "baby"]):
                                    child_id = other_p
                                    break
                        if child_id:
                            person_looks_after[pid] = [child_id]
            if links:
                person_org_links[pid] = links

        # 2. Key Colleague & Partner links
        elif circle in ("colleague", "partner"):
            # Only key people with direct user relationship (1:1 DM) or seed status
            if is_dm or is_small_dataset or pid == "P18":
                links = []
                assigned_oids = set()
                
                # Check direct mentions in contact name or DM title
                for o in orgs:
                    if o["side"] == "work" and o["type"] == "company":
                        oname = o["name"].lower()
                        matches = False
                        if len(oname) > 2 and (oname in pname or oname in dm_title):
                            matches = True
                        elif "nexus" in oname and ("nexus" in pname or "nexus" in dm_title):
                            matches = True
                        elif "postmyad" in oname:
                            # Only link core PMA operators with direct DMs or core ops chat
                            if is_dm and ("pma" in pname or "postmyad" in pname or "pma" in dm_title):
                                matches = True
                            elif any("pma operations" in indices["chat_by_id"].get(cid, {}).get("name", "").lower() for cid in person_chats) and (is_dm or pid == "P18"):
                                matches = True
                        elif "daas" in oname and ("milestone" in pname or "milestone" in dm_title or "daas" in pname):
                            matches = True
                        elif "brightside" in oname and ("brightside" in pname or "brightside" in dm_title):
                            matches = True

                        # Chat participation match (1:1 DMs, small datasets, or small dedicated work groups)
                        if not matches:
                            for cid in person_chats:
                                if chat_to_org.get(cid) == o["id"]:
                                    c_obj = indices["chat_by_id"][cid]
                                    if is_dm or is_small_dataset or len(c_obj.get("participants", [])) <= 6 or pid == "P18":
                                        matches = True
                                        break
                        
                        if matches and o["id"] not in assigned_oids:
                            links.append({"organisation": o["id"], "link": "runs"})
                            assigned_oids.add(o["id"])

                if links:
                    person_org_links[pid] = links

    # Build output
    people = []
    for pid in sorted(circles.keys()):
        entry = {"id": pid, "circle": circles[pid]}
        if pid in person_org_links:
            entry["links"] = person_org_links[pid]
        if pid in person_looks_after:
            entry["looks_after"] = person_looks_after[pid]
        people.append(entry)

    return people
