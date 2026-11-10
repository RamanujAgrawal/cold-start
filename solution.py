#!/usr/bin/env python3
"""Cold Start solution: reads WhatsApp data and produces a structured understanding of a person's life.

    python3 solution.py <input_dir> <output.json>
"""
import json
import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.data_loader import load_input, build_indices
from src.people_classifier import classify_people
from src.org_detector import detect_orgs_and_assign_chats
from src.goal_extractor import extract_goals
from src.loop_detector import detect_loops
from src.people_links import build_people_output


def main(input_dir, output_path):
    start_time = time.time()
    print(f"Loading data from {input_dir}...", file=sys.stderr)
    
    # Stage 0: Load data
    chats, messages, seed = load_input(input_dir)
    indices = build_indices(chats, messages)
    
    print(f"  {len(chats)} chats, {len(messages)} messages, {len(indices['all_people'])} people", file=sys.stderr)
    print(f"  ME = {indices['me_name']}", file=sys.stderr)
    print(f"  Seed: {len(seed.get('circles', {}))} circles, {len(seed.get('organisations', []))} orgs, {len(seed.get('goals', []))} goals", file=sys.stderr)
    
    # Stage 1: Classify people into circles
    print("\nStage 1: Classifying people...", file=sys.stderr)
    circles = classify_people(chats, messages, indices, seed)
    print(f"  Classified {len(circles)} people", file=sys.stderr)
    
    # Stage 2: Detect organisations and assign chats
    print("\nStage 2: Detecting organisations and assigning chats...", file=sys.stderr)
    orgs, chat_ties = detect_orgs_and_assign_chats(chats, messages, indices, seed, circles)
    print(f"  Found {len(orgs)} organisations, assigned {len(chat_ties)} chats", file=sys.stderr)
    
    # Stage 3: Build people with links
    print("\nStage 3: Building people with links...", file=sys.stderr)
    people = build_people_output(circles, orgs, chat_ties, chats, indices)
    print(f"  {len(people)} people with links", file=sys.stderr)
    
    # Stage 4: Extract goals
    print("\nStage 4: Extracting goals...", file=sys.stderr)
    goals = extract_goals(chats, messages, indices, seed, circles)
    print(f"  Found {len(goals)} goals", file=sys.stderr)
    
    # Stage 5: Detect open loops
    print("\nStage 5: Detecting open loops...", file=sys.stderr)
    open_loops = detect_loops(chats, messages, indices, seed, circles)
    print(f"  Found {len(open_loops)} loops", file=sys.stderr)
    
    # Assemble output
    output = {
        "people": people,
        "organisations": orgs,
        "chats": chat_ties,
        "goals": goals,
        "open_loops": open_loops,
    }
    
    # Write output
    Path(output_path).write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    
    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed:.1f}s. Output written to {output_path}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
