#!/usr/bin/env python3
"""Cold Start: Understanding a user's life from their WhatsApp exports.

Usage:
    python solution.py <input_dir> <output.json>
    ./run.sh <input_dir> <output.json>

Pipeline Stages:
  Stage 0: Ingestion, chronological indexing, and graph construction.
  Stage 1: People & relationship circle classification (with Indian cultural grounding).
  Stage 2: Organisation entity discovery and 100% chat binding.
  Stage 3: Organisational links ('runs', 'supports') and childcare responsibilities ('looks_after').
  Stage 4: Personal goal extraction with message evidence arrays.
  Stage 5: Open loops detection, relative due-date parsing, thread resolution, and assistance triage.
"""
import json
import sys
import time
from pathlib import Path

# Add src to module lookup path
sys.path.insert(0, str(Path(__file__).parent))

from src.data_loader import load_input, build_indices
from src.people_classifier import classify_people
from src.org_detector import detect_orgs_and_assign_chats
from src.goal_extractor import extract_goals
from src.loop_detector import detect_loops
from src.people_links import build_people_output


def run_pipeline(input_dir: str, output_path: str):
    """Executes the full end-to-end cold start pipeline."""
    start_time = time.time()
    print(f"Loading data from {input_dir}...", file=sys.stderr)
    
    # -------------------------------------------------------------------------
    # Stage 0: Data Ingestion & Indexing
    # -------------------------------------------------------------------------
    chats, messages, seed = load_input(input_dir)
    indices = build_indices(chats, messages)
    
    print(f"  {len(chats)} chats, {len(messages)} messages, {len(indices['all_people'])} people", file=sys.stderr)
    print(f"  ME = {indices['me_name']}", file=sys.stderr)
    print(f"  Seed: {len(seed.get('circles', {}))} circles, {len(seed.get('organisations', []))} orgs, {len(seed.get('goals', []))} goals", file=sys.stderr)
    
    # -------------------------------------------------------------------------
    # Stage 1: People & Relationship Circles
    # -------------------------------------------------------------------------
    # Resolves all contacts into exactly one circle (spouse, staff, colleague,
    # client, vendor, partner, sibling, parent, relative, friend).
    print("\nStage 1: Classifying people...", file=sys.stderr)
    circles = classify_people(chats, messages, indices, seed)
    print(f"  Classified {len(circles)} people", file=sys.stderr)
    
    # -------------------------------------------------------------------------
    # Stage 2: Organisations & Chat Disambiguation
    # -------------------------------------------------------------------------
    # Discovers companies, workspaces, residences, and vacation estates, then
    # binds every chat to either an organisation or a person.
    print("\nStage 2: Detecting organisations and assigning chats...", file=sys.stderr)
    orgs, chat_ties = detect_orgs_and_assign_chats(chats, messages, indices, seed, circles)
    print(f"  Found {len(orgs)} organisations, assigned {len(chat_ties)} chats", file=sys.stderr)
    
    # -------------------------------------------------------------------------
    # Stage 3: Organisational Links & Responsibilities
    # -------------------------------------------------------------------------
    # Connects core operators ('runs') and domestic staff ('supports') to orgs,
    # and links childcare staff ('looks_after') to children.
    print("\nStage 3: Building people with links...", file=sys.stderr)
    people = build_people_output(circles, orgs, chat_ties, chats, indices)
    print(f"  {len(people)} people with links", file=sys.stderr)
    
    # -------------------------------------------------------------------------
    # Stage 4: Personal Goals & Evidence
    # -------------------------------------------------------------------------
    # Extracts multi-week personal goals across 10 life areas, grounded with
    # real message IDs as verifiable evidence.
    print("\nStage 4: Extracting goals...", file=sys.stderr)
    goals = extract_goals(chats, messages, indices, seed, circles)
    print(f"  Found {len(goals)} goals", file=sys.stderr)
    
    # -------------------------------------------------------------------------
    # Stage 5: Open Loops & Assistance Triage
    # -------------------------------------------------------------------------
    # Identifies commitments, questions, deadlines, and followups. Resolves
    # completed past events vs active projects, and formats assistance actions.
    print("\nStage 5: Detecting open loops...", file=sys.stderr)
    open_loops = detect_loops(chats, messages, indices, seed, circles)
    print(f"  Found {len(open_loops)} loops", file=sys.stderr)
    
    # -------------------------------------------------------------------------
    # Final Output Assembly & JSON Serialization
    # -------------------------------------------------------------------------
    output = {
        "people": people,
        "organisations": orgs,
        "chats": chat_ties,
        "goals": goals,
        "open_loops": open_loops,
    }
    
    output_file = Path(output_path)
    output_file.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    
    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed:.1f}s. Output written to {output_path}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python solution.py <input_dir> <output.json>")
    run_pipeline(sys.argv[1], sys.argv[2])
