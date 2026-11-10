# Cold Start: Understanding a Person's Life from WhatsApp

> **Xpert Winter Internship 2026: The Cold Start Challenge**  
> An autonomous, zero-dependency Python system that parses raw WhatsApp chat exports to build a structured, high-precision understanding of a user's personal and professional life.

---

## 🎯 Overview

Before a personal AI assistant can reliably act for someone on day one, it must understand their world:
1. **People & Circles:** Who each person is, their relationship circle (`spouse`, `child`, `parent`, `sibling`, `relative`, `friend`, `colleague`, `client`, `vendor`, `partner`, `staff`), and caretakers (`looks_after`).
2. **Organisations & Chat Ties:** The companies, workspaces, residential homes, and vacation estates the user runs or belongs to, binding every single chat to an organisation or person.
3. **Personal Goals:** Long-term ambitions ($\le 10$) across 10 life areas with grounded message evidence.
4. **Open Loops:** Active commitments, followups, deadlines, and questions, accurately tracking their status (`open`, `done`, `dropped`) and closing evidence.
5. **Assistance Triage:** Knowing when to proactively `assist` (next actionable step), `remind` (scheduled alert), or `leave` (non-intrusive respect for privacy).

---

## 🚀 Quick Start

### 1. Requirements
- **Python 3.9+** (Standard library only: `json`, `urllib.request`, `re`, `hashlib`, `pathlib`).
- **Zero pip dependencies:** No external packages required.
- An OpenAI-compatible API key (Groq, OpenAI, or local vLLM).

### 2. Configure Environment
Set your API key in `.env` (or set environment variables):
```bash
cp .env.example .env
# Edit .env with your LLM API Key:
# LLM_API_KEY=gsk_...
# LLM_BASE_URL=https://api.groq.com/openai/v1
# LLM_MODEL=qwen/qwen3.8-27b
```
*(Supports comma-separated keys in `LLM_API_KEY` for automatic round-robin pooling and sub-second rate-limit failover).*

### 3. Run the Program
Run the submission script on any dataset directory:
```bash
# On Linux / macOS:
./run.sh example out.json

# On Windows (PowerShell):
python solution.py example out.json
# or:
.\run.ps1 example out.json
```

### 4. Validate & Score Output
Validate the output against the challenge rubric and answer key:
```bash
python check.py example out.json
```

---

## 📁 Repository Structure

```
project 1/
├── src/
│   ├── __init__.py
│   ├── data_loader.py          # Streams JSONL, builds indices & chat graphs
│   ├── llm_client.py           # Multi-key pool, exponential backoff, standard library HTTP
│   ├── people_classifier.py    # Relationship circle classifier with Indian cultural grounding
│   ├── org_detector.py         # Discovers companies, workspaces, residences; binds chats
│   ├── people_links.py         # Maps 'runs', 'supports', and 'looks_after' responsibilities
│   ├── goal_extractor.py       # Detects long-term personal goals with message grounding
│   └── loop_detector.py        # Extracts commitments, temporal aging, and assistance triage
├── example/                    # Starter kit test dataset (Meera)
│   ├── chats.jsonl
│   ├── messages.jsonl
│   ├── seed.json
│   └── answers/
│       └── answers.json
├── ai-logs/                    # Pair programming transcripts & scaling benchmarks
│   └── antigravity_pair_programming.md
├── solution.py                 # Main entry point pipeline
├── run.sh                      # Evaluation runner script: ./run.sh <input_dir> <output.json>
├── run.ps1                     # PowerShell runner script for Windows
├── check.py                    # Official rubric scoring and schema validation script
├── schema.json                 # JSON schema definition
├── WRITEUP.md                  # 2-page challenge writeup (priorities, cost, failure modes)
├── .env.example                # Example environment configuration
├── .gitignore                  # Strict rules excluding private data and secrets
└── README.md
```

---

## 🏗️ Architecture & Pipeline

```
WhatsApp Export (chats.jsonl, messages.jsonl, seed.json)
  │
  ├── Stage 0: Indexing & Graph Construction (Standard Library)
  │     └─► Builds participant graphs, message chronologies, and cultural tokens
  │
  ├── Stage 1: People Classification (Seed Merge + Cultural Regex + Batched LLM)
  │     └─► 12 Circles per person (distinguishing domestic staff from siblings)
  │
  ├── Stage 2: Organisation Clustering & Chat Binding
  │     ├─► Discovers companies, workspaces ('Office'), residence ('Home'), estates ('Farmhouse')
  │     ├─► CamelCase acronym deduplication (e.g. PMA -> PostMyAd)
  │     └─► 100% precision chat binding (separating business DMs from personal chats)
  │
  ├── Stage 3: Organisational Links & Caretakers
  │     ├─► Key leaders mapped to 'runs'
  │     ├─► Domestic/facility staff mapped to 'supports'
  │     └─► Childcare staff mapped to 'looks_after'
  │
  ├── Stage 4: Goal Extraction
  │     └─► Filters personal ambition signals; extracts <= 10 grounded goals
  │
  └── Stage 5: Open Loop Extraction & Assistance Triage
        ├─► Localized participant context window (only chat members injected)
        ├─► Temporal aging (past-dated requests older than 5 days marked 'done')
        └─► Privacy-first triage (assist, remind, or leave)
```

---

## 📊 Benchmark Verification

### `example` Dataset (Meera — Starter Kit)
```
Part                         Score    Points
Circles                       1.00   12.0 / 12
Organisations                 1.00    8.0 / 8
Chats                         1.00    8.0 / 8
Goals                         1.00    8.0 / 8
Open loops found              0.70    9.7 / 14
Open loops closed right       0.80   11.2 / 14
Where to help                 1.00   10.0 / 10
Total from the data                  66.9 / 74 (90.4%)
```

### `data/dev` Dataset (Arjun — 4,502 messages, 138 chats)
```
Part                         Score    Points
Circles                       0.78    9.4 / 12
Organisations                 0.82    6.6 / 8
Chats                         1.00    8.0 / 8
Goals                      not labelled in this set
Open loops found              0.26    3.6 / 14
Open loops closed right       0.20    2.8 / 14
Where to help              not labelled in this set
Total from the data                  30.4 / 56
```
*(Note: Goals and Where to Help carry 0 annotations in dev ground truth; total scored out of 56 available data points).*

---

## 💰 Efficiency & Costs

- **Model:** `qwen/qwen3.8-27b` (via Groq OpenAI-compatible endpoint).
- **Inference Cost:** **$0.0062 per 1,000 messages** (~2.8 cents for 4,500 messages).
- **Runtime:** Under 2 minutes across the 5-key connection pool.
- **Zero Dependencies:** Seamless portability across any clean Python 3.9+ Linux/Windows environment.
