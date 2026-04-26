#!/usr/bin/env python3
"""TechPulse Idea Integration Radar

Reads the latest TechPulse briefing, discovers active OpenClaw projects by
scanning SKILL.md files, and uses a local Ollama model to evaluate each story
for integration opportunities.

Run AFTER techpulse-daily/run.py so we can reuse its story list.

Usage:
    python idea_radar.py                  # Read from techpulse output
    python idea_radar.py --json           # Read from JSON (more reliable parsing)
    python idea_radar.py --no-llm         # Keyword-only matching, no LLM
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
import urllib.request

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

OLLAMA_MODEL = os.environ.get("TECHPULSE_MODEL", "minimax-m2.5:cloud")
OLLAMA_URL = os.environ.get("TECHPULSE_OLLAMA_URL", "http://127.0.0.1:11434/api/generate")

BRIEFING_MD = Path(__file__).parent.parent / "techpulse-daily" / "techpulse_daily_output.md"
BRIEFING_JSON = Path(__file__).parent.parent / "techpulse-daily" / "techpulse_daily_data.json"
OUTPUT_PATH = Path(__file__).with_name("idea_radar_output.md")

MAX_IDEAS = 3
LLM_TIMEOUT = 90

SKILL_DISCOVERY_PATHS = [
    Path.home() / "openclaw-projects" / "skills",
    Path.home() / ".openclaw" / "workspace" / "skills",
    Path.home() / ".openclaw" / "skills",
]

SKIP_PROJECTS = {
    "techpulse-daily",
    "techpulse-daily-idea",
}


# ─────────────────────────────────────────────
# OLLAMA
# ─────────────────────────────────────────────

def call_ollama(prompt: str) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 200},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("response", "").strip()
    except Exception as exc:
        return f"[LLM error: {exc}]"


# ─────────────────────────────────────────────
# PROJECT DISCOVERY
# ─────────────────────────────────────────────

def discover_projects() -> Dict[str, dict]:
    """Scan known paths for SKILL.md files and extract project metadata."""
    projects: Dict[str, dict] = {}
    for base in SKILL_DISCOVERY_PATHS:
        if not base.exists():
            continue
        for skill_dir in base.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            pid = skill_dir.name
            if pid in SKIP_PROJECTS or pid in projects:
                continue
            try:
                content = skill_file.read_text(encoding="utf-8")
            except Exception:
                continue

            # Check if disabled in config
            # (Simple check: look for "enabled: false" pattern)

            # Extract frontmatter
            fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
            name = pid
            description = ""
            if fm_match:
                for line in fm_match.group(1).splitlines():
                    stripped = line.strip()
                    if stripped.startswith("name:"):
                        name = stripped.split(":", 1)[1].strip().strip("\"'")
                    elif stripped.startswith("description:"):
                        desc_val = stripped.split(":", 1)[1].strip().strip("\"'>")
                        if desc_val:
                            description = desc_val

            if not description:
                body = content[fm_match.end():] if fm_match else content
                # Grab purpose or first paragraph
                para = re.search(r"##\s*Purpose\s*\n\n(.+?)(?:\n\n|$)", body, re.DOTALL)
                if not para:
                    para = re.search(r"\n\n(.+?)(?:\n\n|$)", body, re.DOTALL)
                if para:
                    description = para.group(1).replace("\n", " ").strip()[:300]

            # Extract known gaps
            gaps = []
            gap_match = re.search(
                r'(?:known.?gaps|limitations)[:\s]*\n((?:\s*[-*•]\s*.+\n)+)',
                content, re.IGNORECASE
            )
            if gap_match:
                gaps = re.findall(r'[-*•]\s*(.+)', gap_match.group(1))[:5]

            # Extract tech stack keywords
            tech = set()
            for match in re.finditer(r'pip install\s+([^\n]+)', content):
                for pkg in match.group(1).split():
                    if not pkg.startswith('-') and len(pkg) > 2:
                        tech.add(pkg.split('==')[0].lower())

            projects[pid] = {
                "name": name,
                "description": description[:300],
                "known_gaps": gaps,
                "tech_keywords": list(tech)[:10],
            }
    return projects


# ─────────────────────────────────────────────
# PARSE BRIEFING
# ─────────────────────────────────────────────

def parse_from_json(path: Path) -> List[dict]:
    """Parse stories from the JSON output (most reliable)."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("stories", [])
    except Exception as exc:
        sys.stderr.write(f"[IdeaRadar] JSON parse error: {exc}\n")
        return []


def parse_from_markdown(path: Path) -> List[dict]:
    """Parse stories from the markdown briefing."""
    if not path.exists():
        sys.stderr.write(f"[IdeaRadar] Briefing not found: {path}\n")
        return []
    text = path.read_text(encoding="utf-8")
    stories = []

    # Pattern: **Title**\nSummary\n_Impact: TAG_\n[Source](url)
    pattern = re.compile(
        r"\*\*(.+?)\*\*\s*\n(.+?)\n_Impact: (\w+)_\s*\n\[Source\]\((.+?)\)",
        re.DOTALL,
    )
    for m in pattern.finditer(text):
        stories.append({
            "title": m.group(1).strip(),
            "summary": m.group(2).strip(),
            "impact_tag": m.group(3).strip(),
            "url": m.group(4).strip(),
        })
    return stories


# ─────────────────────────────────────────────
# INTEGRATION MATCHING
# ─────────────────────────────────────────────

def build_project_context(projects: Dict[str, dict]) -> str:
    lines = []
    for pid, proj in projects.items():
        line = f"- **{proj['name']}** ({pid}): {proj['description'][:150]}"
        if proj.get("known_gaps"):
            line += f"\n  Gaps: {'; '.join(proj['known_gaps'][:3])}"
        lines.append(line)
    return "\n".join(lines)


def keyword_prefilter(story: dict, projects: Dict[str, dict]) -> bool:
    """Quick check: does the story mention any tech keywords from any project?"""
    text = (story.get("title", "") + " " + story.get("summary", "")).lower()
    for proj in projects.values():
        for kw in proj.get("tech_keywords", []):
            if kw in text:
                return True
        # Also check known gap keywords
        for gap in proj.get("known_gaps", []):
            gap_words = [w.lower() for w in gap.split() if len(w) > 4]
            if any(w in text for w in gap_words[:3]):
                return True
    return False


def evaluate_story(story: dict, project_context: str, valid_targets: set) -> Optional[dict]:
    """Use LLM to evaluate a single story for integration opportunities."""
    prompt = textwrap.dedent(f"""
        You are a pragmatic software architect. Given a news story and a list of
        software projects, decide if the story describes a CONCRETE tool, library,
        API, or technique that can be directly used in one of the projects.

        RULES:
        - Only match if the story mentions a specific open-source library, API,
          dataset, or reusable code that the project could actually import/call.
        - General news about science, policy, breakthroughs, or research does NOT
          count — there must be something a developer can pip install or curl.
        - If unsure, respond SKIP. Most stories should be SKIP.

        PROJECTS:
        {project_context}

        STORY:
        Title: {story.get('title', '')}
        Summary: {story.get('summary', '')}

        Respond with EXACTLY one of:
        SKIP
        or
        TARGET: <project id>
        IDEA: <one sentence>
        EFFORT: LOW | MEDIUM | HIGH
        PRIORITY: HIGH | MEDIUM | LOW
        ACTION: <pip install X / curl Y / etc>
    """).strip()

    result = call_ollama(prompt)
    if "SKIP" in result.upper()[:30]:
        return None

    idea = {"source_title": story.get("title", ""), "source_url": story.get("url", "")}
    for line in result.splitlines():
        line = line.strip()
        key = line.split(":", 1)[0].strip().upper() if ":" in line else ""
        val = line.split(":", 1)[1].strip() if ":" in line else ""
        if key == "TARGET":
            idea["target"] = val
        elif key == "IDEA":
            idea["idea"] = val
        elif key == "EFFORT":
            idea["effort"] = val.split()[0].upper() if val else "MEDIUM"
        elif key == "PRIORITY":
            idea["priority"] = val.split()[0].upper() if val else "MEDIUM"
        elif key == "ACTION":
            idea["action"] = val

    # Validate target
    target = idea.get("target", "").lower().replace(" ", "-")
    if target not in valid_targets:
        return None
    idea["target"] = target

    if idea.get("idea"):
        return idea
    return None


# ─────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────

def format_output(ideas: List[dict]) -> str:
    now = datetime.now(timezone.utc).astimezone()
    date_str = now.strftime("%A, %d %B %Y — %I:%M %p")

    if not ideas:
        return f"# 🔧 Integration Radar — {date_str}\n\nNo integration ideas matched today's stories."

    lines = [f"# 🔧 Integration Radar — {date_str}", ""]
    for i, idea in enumerate(ideas, 1):
        pri = idea.get("priority", "?")
        pri_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(pri, "⚪")
        lines.append(f"**{i}. → {idea.get('target', 'Unknown')}**")
        lines.append(f"💡 {idea.get('idea', '')}")
        lines.append(f"⏱ Effort: {idea.get('effort', '?')} | {pri_emoji} Priority: {pri}")
        lines.append(f"▶️ First step: {idea.get('action', '?')}")
        lines.append(f"📰 From: {idea.get('source_title', '')}")
        lines.append(f"🔗 {idea.get('source_url', '')}")
        lines.append("")

    return "\n".join(lines).strip()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TechPulse Idea Integration Radar")
    parser.add_argument("--json", action="store_true", help="Read from JSON output instead of markdown")
    parser.add_argument("--no-llm", action="store_true", help="Keyword-only matching, skip LLM")
    args = parser.parse_args()

    sys.stderr.write("\n🔧 Idea Integration Radar — Starting\n")
    sys.stderr.write(f"   Model: {OLLAMA_MODEL}\n\n")

    # Discover projects
    projects = discover_projects()
    if not projects:
        sys.stderr.write("[IdeaRadar] No active projects discovered.\n")
        sys.exit(1)
    sys.stderr.write(f"[IdeaRadar] Discovered {len(projects)} projects: {', '.join(projects.keys())}\n")

    # Parse stories
    if args.json and BRIEFING_JSON.exists():
        stories = parse_from_json(BRIEFING_JSON)
        sys.stderr.write(f"[IdeaRadar] Parsed {len(stories)} stories from JSON\n")
    else:
        stories = parse_from_markdown(BRIEFING_MD)
        sys.stderr.write(f"[IdeaRadar] Parsed {len(stories)} stories from markdown\n")

    if not stories:
        sys.stderr.write("[IdeaRadar] No stories found. Run techpulse-daily/run.py first.\n")
        sys.exit(1)

    # Evaluate
    project_context = build_project_context(projects)
    valid_targets = {pid.lower() for pid in projects.keys()}
    ideas: List[dict] = []

    for story in stories:
        if len(ideas) >= MAX_IDEAS:
            break

        # Keyword pre-filter (saves LLM calls)
        if not keyword_prefilter(story, projects):
            continue

        if args.no_llm:
            # Basic keyword match only — report which project's keywords matched
            text = (story.get("title", "") + " " + story.get("summary", "")).lower()
            for pid, proj in projects.items():
                for kw in proj.get("tech_keywords", []):
                    if kw in text:
                        ideas.append({
                            "target": pid,
                            "idea": f"Story mentions '{kw}' which is in {proj['name']}'s tech stack",
                            "effort": "?",
                            "priority": "?",
                            "action": "Review manually",
                            "source_title": story.get("title", ""),
                            "source_url": story.get("url", ""),
                        })
                        break
                if len(ideas) >= MAX_IDEAS:
                    break
        else:
            idea = evaluate_story(story, project_context, valid_targets)
            if idea:
                ideas.append(idea)

    # Output
    output = format_output(ideas)
    OUTPUT_PATH.write_text(output, encoding="utf-8")
    sys.stderr.write(f"[IdeaRadar] Output → {OUTPUT_PATH}\n")
    sys.stderr.write(f"[IdeaRadar] {len(ideas)} idea(s) found\n\n")
    print(output)


if __name__ == "__main__":
    main()
