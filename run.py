#!/usr/bin/env python3
"""TechPulse Daily — Tech & Science News Briefing

Fetches RSS feeds, deduplicates, classifies, scores, summarizes using the
ABT (And-But-Therefore) storytelling framework, and outputs a formatted
briefing readable by secondary school students.

Categories are loaded from categories.json — users can add/remove/modify
categories without editing this script.

Usage:
    python run.py                    # Full pipeline with LLM summaries
    python run.py --no-llm           # Skip LLM, use raw RSS summaries
    python run.py --telegram         # Also send to Telegram
    python run.py --json             # Also output structured JSON
    python run.py --config my.json   # Use custom categories file

Requirements:
    pip install feedparser
    Ollama running locally (or any OpenAI-compatible endpoint)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional

try:
    import feedparser
except ImportError:
    sys.stderr.write("ERROR: feedparser not installed. Run: pip install feedparser\n")
    sys.exit(1)

import llm

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SCRIPT_DIR = Path(__file__).parent
DEFAULT_CONFIG = SCRIPT_DIR / "categories.json"
OUTPUT_MD = SCRIPT_DIR / "techpulse_daily_output.md"
OUTPUT_JSON = SCRIPT_DIR / "techpulse_daily_data.json"

# Impact tag detection — ordered by specificity (most specific first)
IMPACT_TAG_RULES = [
    ("BREAKTHROUGH", ["breakthrough", "first ever", "world record", "first time", "cures", "cure"]),
    ("SECURITY",     ["vulnerability", "cve", "zero-day", "exploit", "ransomware", "rce", "malware"]),
    ("REGULATION",   ["regulation", "eu ai act", "enforcement", "compliance", "penalties", "legislation"]),
    ("MILESTONE",    ["milestone", "record", "surpasses", "outperforms", "state-of-the-art", "achieves", "fidelity"]),
    ("RESEARCH",     ["study", "paper", "researchers", "findings", "published in", "demonstrated", "predicts", "journal"]),
    ("FUNDING",      ["raises", "funding", "acquisition", "ipo", "valuation", "series a", "series b"]),
    ("RELEASE",      ["releases", "launches", "open sources", "announces", "unveils", "released", "shipped", "available"]),
]


# ─────────────────────────────────────────────
# LOAD USER CATEGORIES
# ─────────────────────────────────────────────

def load_config(config_path: Path) -> dict:
    """Load categories.json and build runtime data structures."""
    if not config_path.exists():
        sys.stderr.write(f"FATAL: Config not found: {config_path}\n")
        sys.stderr.write("Run with --config path/to/categories.json or create categories.json in the script directory.\n")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    categories = raw.get("categories", {})
    settings = raw.get("settings", {})

    # Build flattened lookup tables from all categories
    all_feeds = {}
    all_authority = {}
    domain_keywords = {}
    domain_emoji = {}
    domain_minimums = {}

    for cat_name, cat_data in categories.items():
        domain_keywords[cat_name] = cat_data.get("keywords", [])
        domain_emoji[cat_name] = cat_data.get("emoji", "📌")
        domain_minimums[cat_name] = cat_data.get("min_stories", 1)
        for feed_name, feed_url in cat_data.get("feeds", {}).items():
            all_feeds[feed_name] = feed_url
        for src_name, authority in cat_data.get("source_authority", {}).items():
            all_authority[src_name] = authority

    return {
        "categories": categories,
        "feeds": all_feeds,
        "authority": all_authority,
        "domain_keywords": domain_keywords,
        "domain_emoji": domain_emoji,
        "domain_minimums": domain_minimums,
        "total_stories": settings.get("total_stories", 15),
        "flex_slots": settings.get("flex_slots", 3),
        "recency_hours": settings.get("recency_hours", 36),
        "dedup_threshold": settings.get("dedup_threshold", 0.80),
        "feed_timeout": settings.get("feed_timeout_seconds", 20),
        "max_workers": settings.get("max_workers", 10),
        "llm_config": llm.LLMConfig.from_settings(settings),
    }


# ─────────────────────────────────────────────
# STEP 1: INGEST
# ─────────────────────────────────────────────

def fetch_single_feed(name: str, url: str, timeout: int = 20) -> List[dict]:
    """Fetch and parse a single RSS feed."""
    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            return []
        articles = []
        for entry in feed.entries[:20]:
            published = None
            for date_field in ("published_parsed", "updated_parsed"):
                tp = entry.get(date_field)
                if tp:
                    try:
                        published = datetime(*tp[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass
                    break
            if not published:
                published = datetime.now(timezone.utc)

            title = entry.get("title", "").strip()
            if not title:
                continue

            summary = entry.get("summary", "") or entry.get("description", "")
            summary = re.sub(r"<[^>]+>", " ", summary)
            summary = re.sub(r"\s+", " ", summary).strip()[:500]

            articles.append({
                "title": title,
                "url": entry.get("link", ""),
                "source": name,
                "published": published.isoformat(),
                "published_dt": published,
                "raw_summary": summary,
            })
        return articles
    except Exception as exc:
        sys.stderr.write(f"  [WARN] Feed failed: {name} — {exc}\n")
        return []


def ingest_all_feeds(config: dict) -> List[dict]:
    """Fetch all RSS feeds concurrently."""
    feeds = config["feeds"]
    sys.stderr.write(f"[Step 1] Ingesting {len(feeds)} feeds...\n")
    all_articles = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=config["max_workers"]) as executor:
        future_map = {
            executor.submit(fetch_single_feed, name, url, config["feed_timeout"]): name
            for name, url in feeds.items()
        }
        for future in concurrent.futures.as_completed(future_map, timeout=config["feed_timeout"] + 15):
            name = future_map[future]
            try:
                articles = future.result(timeout=config["feed_timeout"])
                all_articles.extend(articles)
            except Exception as exc:
                sys.stderr.write(f"  [WARN] {name}: {exc}\n")
    sys.stderr.write(f"  Ingested {len(all_articles)} raw articles\n")
    return all_articles


# ─────────────────────────────────────────────
# STEP 2: NORMALIZE & DEDUPLICATE
# ─────────────────────────────────────────────

def normalize_and_dedup(articles: List[dict], config: dict) -> List[dict]:
    """Filter by recency, deduplicate by URL then title similarity."""
    sys.stderr.write("[Step 2] Normalizing & deduplicating...\n")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config["recency_hours"])

    recent = []
    for a in articles:
        try:
            pub = a.get("published_dt") or datetime.fromisoformat(a["published"])
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            if pub >= cutoff:
                a["published_dt"] = pub
                recent.append(a)
        except Exception:
            a["published_dt"] = datetime.now(timezone.utc)
            recent.append(a)

    # URL dedup
    seen_urls = set()
    url_deduped = []
    for a in recent:
        url_norm = a["url"].split("?")[0].rstrip("/").lower()
        if url_norm and url_norm not in seen_urls:
            seen_urls.add(url_norm)
            url_deduped.append(a)

    # Title similarity dedup
    authority = config["authority"]
    threshold = config["dedup_threshold"]
    final = []
    for a in url_deduped:
        is_dup = False
        for existing in final:
            ratio = SequenceMatcher(None, a["title"].lower(), existing["title"].lower()).ratio()
            if ratio > threshold:
                if authority.get(a["source"], 3) > authority.get(existing["source"], 3):
                    final.remove(existing)
                    final.append(a)
                is_dup = True
                break
        if not is_dup:
            final.append(a)

    sys.stderr.write(f"  {len(recent)} recent → {len(final)} after dedup\n")
    return final


# ─────────────────────────────────────────────
# STEP 3: CLASSIFY
# ─────────────────────────────────────────────

def classify_domain(article: dict, config: dict) -> str:
    """Classify article into a category by keyword frequency."""
    text = (article["title"] + " " + article["raw_summary"]).lower()
    scores = {}
    for domain, keywords in config["domain_keywords"].items():
        scores[domain] = sum(1 for kw in keywords if kw in text)
    if not scores or max(scores.values()) == 0:
        # Default to the last category defined (usually the catch-all)
        return list(config["domain_keywords"].keys())[-1]
    return max(scores, key=scores.get)


def classify_all(articles: List[dict], config: dict) -> List[dict]:
    sys.stderr.write("[Step 3] Classifying domains...\n")
    for a in articles:
        a["domain"] = classify_domain(a, config)
    counts = {}
    for a in articles:
        counts[a["domain"]] = counts.get(a["domain"], 0) + 1
    sys.stderr.write(f"  Distribution: {counts}\n")
    return articles


# ─────────────────────────────────────────────
# STEP 4: SCORE
# ─────────────────────────────────────────────

def score_article(article: dict, all_articles: List[dict], config: dict) -> float:
    """Composite relevance score (0–10)."""
    score = 0.0
    authority = config["authority"]

    # Source authority (30%)
    auth = authority.get(article["source"], 3)
    score += auth * 0.30

    # Recency (25%)
    try:
        hours_old = (datetime.now(timezone.utc) - article["published_dt"]).total_seconds() / 3600
    except Exception:
        hours_old = 24
    recency = 10 if hours_old <= 6 else 8 if hours_old <= 12 else 5 if hours_old <= 24 else 2
    score += recency * 0.25

    # Cross-source mentions (20%)
    similar = sum(
        1 for other in all_articles
        if other["url"] != article["url"]
        and SequenceMatcher(None, article["title"].lower(), other["title"].lower()).ratio() > 0.6
    )
    score += min(similar * 3, 10) * 0.20

    # Impact keywords (15%)
    text = (article["title"] + " " + article["raw_summary"]).lower()
    impact = 0
    for imp_score, keywords in [(10, ["breakthrough", "first ever", "world record"]),
                                 (7, ["significant", "major", "milestone", "surpasses"]),
                                 (4, ["announces", "releases", "launches", "open sources"]),
                                 (1, ["update", "patch", "minor", "preview"])]:
        if any(kw in text for kw in keywords):
            impact = max(impact, imp_score)
    score += impact * 0.15

    # Domain balance (10%)
    domain_counts = {}
    for a in all_articles:
        d = a.get("domain", "")
        domain_counts[d] = domain_counts.get(d, 0) + 1
    if domain_counts.get(article.get("domain", ""), 99) < 5:
        score += 5 * 0.10

    return round(score, 2)


def score_all(articles: List[dict], config: dict) -> List[dict]:
    sys.stderr.write("[Step 4] Scoring articles...\n")
    for a in articles:
        a["score"] = score_article(a, articles, config)
    articles.sort(key=lambda x: x["score"], reverse=True)
    sys.stderr.write(f"  Top score: {articles[0]['score'] if articles else 0}\n")
    return articles


# ─────────────────────────────────────────────
# STEP 5: SELECT TOP N
# ─────────────────────────────────────────────

def select_top(articles: List[dict], config: dict) -> List[dict]:
    total = config["total_stories"]
    minimums = config["domain_minimums"]
    sys.stderr.write(f"[Step 5] Selecting top {total} stories...\n")

    selected = []
    remaining = list(articles)

    # Pass 1: Fill category minimums
    for domain, min_count in minimums.items():
        domain_stories = [a for a in remaining if a.get("domain") == domain]
        for story in domain_stories[:min_count]:
            selected.append(story)
            remaining.remove(story)

    # Pass 2: Fill flex slots with highest-scoring remaining
    flex_count = total - len(selected)
    remaining.sort(key=lambda x: x.get("score", 0), reverse=True)
    selected.extend(remaining[:max(0, flex_count)])

    selected = selected[:total]
    sys.stderr.write(f"  Selected {len(selected)} stories\n")
    return selected


# ─────────────────────────────────────────────
# STEP 6: SUMMARIZE (LLM — ABT Framework)
# ─────────────────────────────────────────────

# ── LLM SUMMARIZATION PROMPT ──
# Uses ABT (And, But, Therefore) + Analogy Bridging
# Designed to produce identical output format regardless of which model runs it.

ABT_SYSTEM_PROMPT = """You are a news translator. Your job is to rewrite technical news into short stories that a 14-year-old student can understand.

RULES YOU MUST FOLLOW EXACTLY:
1. Write EXACTLY 3 to 5 sentences. No more, no less.
2. Use the ABT storytelling structure:
   - Sentence 1-2 (AND): Set the scene. Explain what normally happens or what the background is. Use an everyday analogy if the topic is technical.
   - Sentence 3 (BUT): Introduce the news — what changed, what's new, what's the problem or breakthrough.
   - Sentence 4-5 (THEREFORE): Explain why it matters to ordinary people. What's the real-world impact.
3. NO jargon. Replace every technical term with a simple explanation or analogy.
   - BAD: "achieved 99.9% two-qubit gate fidelity"
   - GOOD: "got their quantum computer to work correctly 999 times out of 1000"
   - BAD: "mixture-of-experts architecture with 109B parameters"
   - GOOD: "a brain with 109 billion connections that only uses a small part at a time, like reading one chapter of a library instead of the whole thing"
4. Include specific numbers, names, and facts from the original. Do not make up numbers.
5. Do NOT start with "In a world where" or "Imagine a world" or any cliché opening.
6. Do NOT use bullet points, headers, or labels like "AND:", "BUT:", "THEREFORE:".
7. Write as a single flowing paragraph.
8. Keep the total length under 100 words."""

ABT_USER_TEMPLATE = """Rewrite this news story for a 14-year-old student. Follow the rules exactly.

Title: {title}
Original: {content}

Your rewrite:"""


def _sanitize_summary(text: str) -> str:
    """Strip model artifacts and flatten to a single ABT paragraph."""
    text = re.sub(r"^(AND|BUT|THEREFORE|Summary|Rewrite|Here)[:\s]*", "", text, flags=re.I)
    text = re.sub(r"\*\*.*?\*\*", "", text)  # Remove markdown bold
    text = re.sub(r"^[-•]\s*", "", text, flags=re.M)  # Remove bullets
    text = re.sub(r"\n+", " ", text)  # Flatten to single paragraph
    return re.sub(r"\s+", " ", text).strip()


def summarize_article_abt(article: dict, config: dict) -> str:
    """Generate ABT-style summary via LLM, with fallbacks."""
    raw = article.get("raw_summary", "")

    # Try LLM first (retries + backoff handled by the client)
    prompt = ABT_USER_TEMPLATE.format(
        title=article["title"],
        content=raw[:500],
    )
    result = _sanitize_summary(config["llm_config"].complete(ABT_SYSTEM_PROMPT, prompt))

    # Validate: must be 50+ chars, 2+ sentences
    if result and len(result) > 50 and result.count(".") >= 2:
        return result[:500]

    # Fallback: clean raw summary (3 sentences max)
    if raw:
        clean = re.sub(r"\s+", " ", raw).strip()
        sentences = re.split(r"(?<=[.!?])\s+", clean)
        return " ".join(sentences[:3])[:400]

    return article["title"]


def summarize_raw_fallback(article: dict) -> str:
    """No-LLM fallback: clean and truncate raw RSS summary."""
    raw = article.get("raw_summary", "")
    if raw:
        clean = re.sub(r"\s+", " ", raw).strip()
        sentences = re.split(r"(?<=[.!?])\s+", clean)
        return " ".join(sentences[:3])[:400]
    return article["title"]


def detect_impact_tag(article: dict) -> str:
    """Determine impact tag from content. Most specific tags checked first."""
    text = (article["title"] + " " + article.get("raw_summary", "")).lower()
    for tag, keywords in IMPACT_TAG_RULES:
        if any(kw in text for kw in keywords):
            return tag
    return "UPDATE"


def format_timestamp(article: dict) -> str:
    """Format the publication timestamp as 'DD Mon YYYY'."""
    try:
        dt = article.get("published_dt")
        if not dt:
            dt = datetime.fromisoformat(article["published"])
        return dt.strftime("%d %b %Y")
    except Exception:
        return "Unknown date"


def summarize_all(articles: List[dict], use_llm: bool, config: dict) -> List[dict]:
    sys.stderr.write(f"[Step 6] Summarizing {len(articles)} stories (LLM={'ON' if use_llm else 'OFF'})...\n")
    llm_calls = 0
    for a in articles:
        if use_llm:
            a["summary"] = summarize_article_abt(a, config)
            llm_calls += 1
        else:
            a["summary"] = summarize_raw_fallback(a)
        a["impact_tag"] = detect_impact_tag(a)
        a["timestamp"] = format_timestamp(a)
    sys.stderr.write(f"  LLM calls: {llm_calls}\n")
    return articles


# ─────────────────────────────────────────────
# STEP 7: FORMAT
# ─────────────────────────────────────────────

def format_briefing(articles: List[dict], stats: dict, config: dict) -> str:
    """Format the daily briefing as markdown with timestamps."""
    now = datetime.now(timezone.utc).astimezone()
    date_str = now.strftime("%a, %d %b %Y")
    total = len(articles)
    cat_count = len(set(a.get("domain", "") for a in articles))

    lines = [
        f"📡 **TechPulse Daily** — {date_str}",
        f"Top {total} stories across {cat_count} categories",
        "━" * 36,
        "",
    ]

    # Group by domain in config order (preserves user-defined ordering)
    domain_order = list(config["domain_keywords"].keys())
    for domain in domain_order:
        emoji = config["domain_emoji"].get(domain, "📌")
        domain_stories = [a for a in articles if a.get("domain") == domain]
        if not domain_stories:
            continue

        lines.append(f"{emoji} **{domain}**")
        lines.append("")
        for a in domain_stories:
            lines.append(f"▸ **{a['title']}**")
            lines.append(f"📅 {a.get('timestamp', '')} · {a['source']}")
            lines.append("")
            lines.append(f"{a.get('summary', a['title'])}")
            lines.append("")
            lines.append(f"`{a.get('impact_tag', 'UPDATE')}` · [Source]({a['url']})")
            lines.append("")

    lines.append("━" * 36)
    lines.append(f"📊 Sources: {stats.get('feeds_ok', 0)}/{stats.get('feeds_total', 0)} feeds")
    lines.append(f"📰 Evaluated: {stats.get('total_candidates', 0)} → Selected: {stats.get('selected', 0)}")
    lines.append(f"⏱ Pipeline: {stats.get('elapsed_sec', 0):.1f}s")
    lines.append(f"🧠 Model: {config['llm_config'].describe()}")
    lines.append(f"📖 Storytelling: ABT (And-But-Therefore) + Analogy Bridging")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# STEP 8: DELIVER
# ─────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    """Send briefing via Telegram. Split if > 4096 chars."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        sys.stderr.write("  [Telegram] No bot token or chat ID. Skipping.\n")
        return False

    MAX_LEN = 4000
    if len(text) <= MAX_LEN:
        chunks = [text]
    else:
        # Split by category sections
        parts = re.split(r"(?=(?:🤖|💻|⚛️|🧬|🚀|📌) \*\*)", text)
        chunks = []
        current = ""
        for part in parts:
            if len(current) + len(part) > MAX_LEN:
                if current:
                    chunks.append(current)
                current = part
            else:
                current += part
        if current:
            chunks.append(current)

    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        data = json.dumps(payload).encode("utf-8")
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    sys.stderr.write(f"  [Telegram] API error: {result}\n")
                    return False
        except Exception as exc:
            sys.stderr.write(f"  [Telegram] Send failed: {exc}\n")
            return False

    sys.stderr.write(f"  [Telegram] Sent {len(chunks)} message(s)\n")
    return True


def save_json(articles: List[dict], stats: dict, config: dict):
    """Save structured data for downstream consumers."""
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": config["llm_config"].describe(),
        "storytelling_framework": "ABT (And-But-Therefore) + Analogy Bridging",
        "categories": list(config["domain_keywords"].keys()),
        "stats": stats,
        "stories": [
            {
                "title": a["title"],
                "url": a["url"],
                "source": a["source"],
                "domain": a.get("domain", ""),
                "score": a.get("score", 0),
                "summary": a.get("summary", ""),
                "impact_tag": a.get("impact_tag", ""),
                "published": a.get("published", ""),
                "timestamp": a.get("timestamp", ""),
            }
            for a in articles
        ],
    }
    OUTPUT_JSON.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    sys.stderr.write(f"[Output] JSON → {OUTPUT_JSON}\n")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run_pipeline(
    config_path: Path | str = DEFAULT_CONFIG,
    use_llm: bool = True,
    telegram: bool = False,
    json_out: bool = False,
) -> tuple[str, dict]:
    """Run the full briefing pipeline once and return ``(briefing, stats)``.

    This is the single entry point shared by the CLI (``main``) and the local
    application/scheduler (``app.py``), so both paths behave identically.
    """
    config = load_config(Path(config_path))

    t0 = time.time()
    cat_names = list(config["domain_keywords"].keys())
    sys.stderr.write(f"\n📡 TechPulse Daily — Starting pipeline\n")
    sys.stderr.write(f"   Model: {config['llm_config'].describe()}\n")
    sys.stderr.write(f"   Categories: {', '.join(cat_names)}\n")
    sys.stderr.write(f"   Config: {config_path}\n")
    sys.stderr.write(f"   Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

    # Step 1: Ingest
    raw_articles = ingest_all_feeds(config)
    feeds_ok = len(set(a["source"] for a in raw_articles))
    if not raw_articles:
        raise RuntimeError("No articles ingested. Check network and feed URLs.")

    # Step 2: Normalize & Dedup
    articles = normalize_and_dedup(raw_articles, config)

    # Step 3: Classify
    articles = classify_all(articles, config)

    # Step 4: Score
    articles = score_all(articles, config)
    total_candidates = len(articles)

    # Step 5: Select
    selected = select_top(articles, config)

    # Step 6: Summarize
    selected = summarize_all(selected, use_llm=use_llm, config=config)

    elapsed = time.time() - t0
    stats = {
        "feeds_total": len(config["feeds"]),
        "feeds_ok": feeds_ok,
        "total_candidates": total_candidates,
        "selected": len(selected),
        "elapsed_sec": elapsed,
    }

    # Step 7: Format
    briefing = format_briefing(selected, stats, config)

    # Step 8: Deliver
    OUTPUT_MD.write_text(briefing, encoding="utf-8")
    sys.stderr.write(f"[Output] Markdown → {OUTPUT_MD}\n")

    if json_out:
        save_json(selected, stats, config)

    if telegram:
        sys.stderr.write("[Step 8] Sending to Telegram...\n")
        send_telegram(briefing)

    sys.stderr.write(f"\n✅ Pipeline complete in {elapsed:.1f}s\n")
    sys.stderr.write(f"   {feeds_ok}/{len(config['feeds'])} feeds responded\n")
    sys.stderr.write(f"   {total_candidates} candidates → {len(selected)} selected\n")
    sys.stderr.write(f"   Categories: {', '.join(cat_names)}\n\n")

    return briefing, stats


def main():
    parser = argparse.ArgumentParser(description="TechPulse Daily — Tech & Science News Briefing")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM, use raw RSS summaries")
    parser.add_argument("--telegram", action="store_true", help="Send to Telegram")
    parser.add_argument("--json", action="store_true", help="Output structured JSON")
    parser.add_argument("--config", type=str, default=str(DEFAULT_CONFIG), help="Path to categories.json")
    args = parser.parse_args()

    try:
        briefing, _ = run_pipeline(
            config_path=args.config,
            use_llm=not args.no_llm,
            telegram=args.telegram,
            json_out=args.json,
        )
    except RuntimeError as exc:
        sys.stderr.write(f"FATAL: {exc}\n")
        sys.exit(1)

    print(briefing)


if __name__ == "__main__":
    main()
