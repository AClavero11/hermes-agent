#!/usr/bin/env python3
"""
X/Twitter Scraper Tool for Hermes

Fetches tweet content from X.com/Twitter URLs using tiered fallbacks:
1. Apify actor (quacker~twitter-url-scraper) — structured data with metrics
2. Jina Reader on x.com — free, reliable markdown extraction
3. fxtwitter API — lightweight fallback

Used by:
- gateway/url_enrichment.py (auto-enrichment pipeline)
- LLM tool calls (x_scrape tool)
"""

import json
import logging
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

XURL_TIMEOUT = 8
APIFY_TIMEOUT = 30
JINA_TIMEOUT = 10
FXTWITTER_TIMEOUT = 10
MAX_WORKERS = 8
MAX_TWEET_CHARS = 5000

# Normalize any Twitter-like URL to x.com canonical form
_TWITTER_URL_RE = re.compile(
    r'https?://(?:(?:www|mobile)\.)?(?:twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|t\.co)/(.+)',
    re.IGNORECASE,
)


def _normalize_url(url: str) -> str:
    """Convert any Twitter URL variant to canonical x.com URL."""
    m = _TWITTER_URL_RE.match(url)
    if m:
        return f"https://x.com/{m.group(1)}"
    return url


def _extract_status_id(url: str) -> Optional[str]:
    """Extract a tweet/post status ID from a Twitter/X URL."""
    m = re.search(r"/status(?:es)?/(\d+)", url)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{5,}", url.strip()):
        return url.strip()
    return None


def _has_external_link(content: str) -> bool:
    """Return True when content includes a non-X URL worth preserving."""
    for match in re.finditer(r"https?://\S+", content):
        host = match.group(0).lower()
        if not any(domain in host for domain in ("x.com", "twitter.com", "t.co", "fxtwitter.com", "vxtwitter.com")):
            return True
    return False


def _score_content(content: Optional[str]) -> int:
    """Score scraped content by usefulness, not just length."""
    if not content:
        return 0
    score = min(len(content), 1600)
    if _has_external_link(content):
        score += 500
    if "Engagement:" in content:
        score += 150
    boilerplate_markers = ("New to X?", "Trending now", "What's happening", "Don’t miss what's happening")
    if any(marker in content for marker in boilerplate_markers):
        score -= 600
    return score


def _scrape_via_xurl(url: str) -> Optional[str]:
    """Use the official xurl CLI when it is configured and has API credits."""
    xurl_path = shutil.which("xurl")
    if not xurl_path:
        return None

    status_id = _extract_status_id(url)
    target = status_id or _normalize_url(url)

    try:
        proc = subprocess.run(
            [xurl_path, "read", target],
            capture_output=True,
            text=True,
            timeout=XURL_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("xurl scrape failed for %s: %s", url, exc)
        return None

    output = (proc.stdout or "").strip()
    error_output = (proc.stderr or "").strip()
    combined = "\n".join(part for part in (output, error_output) if part).strip()

    if proc.returncode != 0:
        if "CreditsDepleted" in combined or "does not have any credits" in combined:
            logger.debug("xurl credits depleted for %s", url)
        else:
            logger.debug("xurl returned %s for %s: %s", proc.returncode, url, combined[:200])
        return None

    if not combined:
        return None

    try:
        data = json.loads(combined)
        if isinstance(data, dict):
            text = data.get("text") or data.get("full_text") or data.get("body")
            author = data.get("author") or data.get("user") or {}
            parts = []
            if isinstance(author, dict):
                handle = author.get("username") or author.get("userName") or author.get("screen_name")
                name = author.get("name")
                if handle and name:
                    parts.append(f"@{handle} ({name})")
                elif handle:
                    parts.append(f"@{handle}")
            if text:
                parts.append(str(text))
            if parts:
                return "\n\n".join(parts)
    except json.JSONDecodeError:
        pass

    return combined[:MAX_TWEET_CHARS]


# ---------------------------------------------------------------------------
# Tier 1: Apify (structured, best quality)
# ---------------------------------------------------------------------------

def _scrape_via_apify(urls: List[str]) -> Optional[List[Dict[str, Any]]]:
    """Scrape tweets via Apify actor (synchronous run)."""
    api_key = os.getenv("APIFY_API_KEY", "")
    if not api_key:
        logger.debug("APIFY_API_KEY not set, skipping Apify")
        return None

    canonical = [_normalize_url(u) for u in urls]
    payload = {
        "startUrls": [{"url": u} for u in canonical],
        "maxItems": len(canonical) * 2,
        "addUserInfo": True,
    }

    try:
        resp = requests.post(
            "https://api.apify.com/v2/acts/quacker~twitter-url-scraper/run-sync-get-dataset-items",
            json=payload,
            params={"token": api_key, "timeout": APIFY_TIMEOUT},
            timeout=APIFY_TIMEOUT + 10,
        )

        if resp.status_code == 200:
            items = resp.json()
            if items:
                return items
            logger.debug("Apify returned empty dataset")
            return None

        logger.warning("Apify returned %d: %s", resp.status_code, resp.text[:200])
        return None
    except requests.Timeout:
        logger.warning("Apify timed out after %ds", APIFY_TIMEOUT)
        return None
    except Exception as e:
        logger.warning("Apify scrape failed: %s", e)
        return None


def _format_apify_tweet(item: Dict[str, Any]) -> str:
    """Format a single Apify tweet result into readable text."""
    parts = []

    author = item.get("author", {})
    name = author.get("name", item.get("user", {}).get("name", "Unknown"))
    handle = author.get("userName", item.get("user", {}).get("screen_name", ""))
    if handle:
        parts.append(f"@{handle} ({name})")
    else:
        parts.append(name)

    created = item.get("createdAt", item.get("created_at", ""))
    if created:
        parts.append(f"Posted: {created}")

    text = item.get("text", item.get("full_text", ""))
    if text:
        parts.append(f"\n{text}")

    likes = item.get("likeCount", item.get("favorite_count", 0))
    retweets = item.get("retweetCount", item.get("retweet_count", 0))
    replies = item.get("replyCount", 0)
    views = item.get("viewCount", item.get("views_count", 0))
    bookmarks = item.get("bookmarkCount", 0)

    metrics = []
    if views:
        metrics.append(f"{views:,} views")
    if likes:
        metrics.append(f"{likes:,} likes")
    if retweets:
        metrics.append(f"{retweets:,} RTs")
    if replies:
        metrics.append(f"{replies:,} replies")
    if bookmarks:
        metrics.append(f"{bookmarks:,} bookmarks")
    if metrics:
        parts.append(f"Engagement: {' | '.join(metrics)}")

    quoted = item.get("quoted_tweet") or item.get("quotedTweet")
    if quoted:
        qt_text = quoted.get("text", quoted.get("full_text", ""))
        qt_author = quoted.get("author", {}).get("userName", "")
        if qt_text:
            parts.append(f"\n> Quoting @{qt_author}: {qt_text}")

    media = item.get("media", item.get("entities", {}).get("media", []))
    if media:
        media_types = [m.get("type", "media") for m in media if isinstance(m, dict)]
        if media_types:
            parts.append(f"Media: {', '.join(media_types)}")

    is_reply = item.get("isReply", False) or item.get("in_reply_to_status_id")
    if is_reply:
        reply_to = item.get("inReplyToUsername", item.get("in_reply_to_screen_name", ""))
        if reply_to:
            parts.append(f"Reply to: @{reply_to}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tier 2: Jina Reader on x.com (free, reliable)
# ---------------------------------------------------------------------------

def _clean_jina_content(content: str) -> Optional[str]:
    """Trim X page chrome from Jina markdown while preserving tweet text."""
    content = content.strip()
    content = re.sub(r'!\[Image \d+(?::[^\]]*)?\]\([^)]*\)\n*', '', content)

    lines = content.split('\n')
    if any(line.strip() in ("## Conversation", "# Conversation") for line in lines):
        for idx, line in enumerate(lines):
            if line.strip() in ("## Conversation", "# Conversation"):
                lines = lines[idx + 1:]
                break

    cleaned = []
    skip_exact = {
        "Markdown Content:",
        "Don't miss what's happening",
        "Don’t miss what’s happening",
        "People on X are the first to know.",
        "See new posts",
        "# [](https://x.com/)",
        "## [](https://x.com/)",
        "# Post",
        "## Post",
        "More",
        "|",
    }
    skip_prefixes = (
        "Warning:",
        "URL Source:",
        "Published Time:",
        "Title:",
        "Sign in",
        "Sign up",
        "Relevant people",
        "What's happening",
        "What’s happening",
        "Show more",
        "Terms of Service",
        "Cookie Policy",
        "Privacy Policy",
        "Accessibility",
        "© ",
        "Footer navigation",
        "Primary navigation",
    )
    stop_markers = {
        "## New to X?",
        "# New to X?",
        "## Trending now",
        "# Trending now",
        "Something went wrong. Try reloading.",
        "Retry",
    }

    for line in lines:
        stripped = line.strip()
        if stripped in stop_markers:
            break
        if not stripped:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        if stripped in skip_exact:
            continue
        if any(stripped.startswith(prefix) for prefix in skip_prefixes):
            continue
        if stripped.startswith("# "):
            continue
        if stripped.startswith("[") and stripped.endswith(")") and "http" in stripped:
            continue
        if re.fullmatch(r"[·\d,\s]+", stripped):
            continue
        cleaned.append(line)

    result = "\n".join(cleaned).strip()
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result[:MAX_TWEET_CHARS] if len(result) > 20 else None


def _scrape_via_jina(url: str) -> Optional[str]:
    """Fetch tweet content via Jina Reader API on the canonical x.com URL."""
    canonical = _normalize_url(url)

    try:
        resp = requests.get(
            f"https://r.jina.ai/{canonical}",
            headers={"Accept": "text/markdown", "X-No-Cache": "true"},
            timeout=JINA_TIMEOUT,
        )

        if resp.status_code == 200 and resp.text.strip():
            result = _clean_jina_content(resp.text)
            if result:
                return result
            logger.debug("Jina content too short after cleaning")
            return None

        logger.debug("Jina returned %d for %s", resp.status_code, canonical)
        return None
    except requests.Timeout:
        logger.debug("Jina timed out for %s", url)
        return None
    except Exception as e:
        logger.debug("Jina scrape failed for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Tier 3: fxtwitter API (lightweight fallback)
# ---------------------------------------------------------------------------

def _scrape_via_fxtwitter(url: str) -> Optional[str]:
    """Fallback: fetch tweet via fxtwitter API."""
    canonical = _normalize_url(url)
    fx_url = canonical.replace("https://x.com/", "https://api.fxtwitter.com/")

    try:
        resp = requests.get(fx_url, timeout=FXTWITTER_TIMEOUT)
        if resp.status_code != 200:
            return None

        data = resp.json()
        tweet = data.get("tweet")
        if not tweet:
            return None

        parts = []
        author = tweet.get("author", {})
        parts.append(f"@{author.get('screen_name', '?')} ({author.get('name', '?')})")

        text = tweet.get("text", "")
        if text:
            parts.append(f"\n{text}")

        likes = tweet.get("likes", 0)
        retweets = tweet.get("retweets", 0)
        replies = tweet.get("replies", 0)
        metrics = []
        if likes:
            metrics.append(f"{likes:,} likes")
        if retweets:
            metrics.append(f"{retweets:,} RTs")
        if replies:
            metrics.append(f"{replies:,} replies")
        if metrics:
            parts.append(f"Engagement: {' | '.join(metrics)}")

        return "\n".join(parts)
    except Exception as e:
        logger.debug("fxtwitter fallback failed for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _scrape_one(url: str) -> Dict[str, str]:
    """Scrape one URL through all available tiers and return a structured result."""
    canonical = _normalize_url(url)

    candidates = []
    xurl_content = _scrape_via_xurl(canonical)
    if xurl_content:
        candidates.append(("xurl", xurl_content))

    fx_content = _scrape_via_fxtwitter(canonical)
    if fx_content:
        candidates.append(("fxtwitter", fx_content))

    jina_content = _scrape_via_jina(canonical)
    if jina_content:
        candidates.append(("jina", jina_content))

    if not candidates:
        return {"url": url, "source": "none", "error": "all tiers failed"}

    source, content = max(candidates, key=lambda item: _score_content(item[1]))
    return {"url": url, "source": source, "content": content}


def x_scrape_tool(urls: List[str]) -> str:
    """
    Scrape X/Twitter URLs and return content as JSON string.

    Tries: Apify batch -> xurl/fxtwitter/Jina per URL in parallel

    Args:
        urls: List of Twitter/X URLs to scrape

    Returns:
        JSON string with {"content": "..."} or {"error": "..."}
    """
    if not urls:
        return json.dumps({"error": "No URLs provided"})

    # Tier 1: Apify (batch, structured)
    apify_results = _scrape_via_apify(urls)
    if apify_results:
        formatted = [_format_apify_tweet(item) for item in apify_results]
        content = "\n\n---\n\n".join(formatted)
        return json.dumps({"content": content, "source": "apify", "count": len(apify_results)})

    # Tier 2+: per-URL fallbacks run in parallel so multi-link research prompts
    # don't pay one full Jina timeout per tweet.
    results_by_index: Dict[int, Dict[str, str]] = {}
    worker_count = min(MAX_WORKERS, len(urls))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(_scrape_one, url): idx for idx, url in enumerate(urls)}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results_by_index[idx] = future.result()
            except Exception as exc:
                logger.debug("Tweet scrape worker failed for %s: %s", urls[idx], exc)
                results_by_index[idx] = {"url": urls[idx], "source": "none", "error": str(exc)}

    results = [
        results_by_index[idx]
        for idx in range(len(urls))
        if results_by_index.get(idx, {}).get("content")
    ]

    if results:
        combined = "\n\n---\n\n".join(
            f"Source URL: {r['url']}\nScrape source: {r['source']}\n\n{r['content']}"
            for r in results
        )
        sources = sorted(set(r["source"] for r in results))
        return json.dumps({
            "content": combined,
            "source": ",".join(sources),
            "count": len(results),
            "results": results,
        }, ensure_ascii=False)

    failures = [
        results_by_index[idx]
        for idx in range(len(urls))
        if results_by_index.get(idx, {}).get("error")
    ]
    return json.dumps({
        "error": f"Failed to scrape {len(urls)} URL(s) - all tiers exhausted (Apify/xurl/fxtwitter/Jina)",
        "failures": failures,
    }, ensure_ascii=False)


def _x_scrape_handler(args: Dict[str, Any], **kwargs) -> str:
    """Registry handler wrapper."""
    urls = args.get("urls", [])
    if isinstance(urls, str):
        urls = [urls]
    return x_scrape_tool(urls)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry  # noqa: E402

X_SCRAPE_SCHEMA = {
    "name": "x_scrape",
    "description": (
        "Scrape content from X.com/Twitter URLs. Fetches tweet text, author, "
        "engagement metrics (likes, RTs, views), media info, and thread context. "
        "Use for any x.com, twitter.com, t.co, fxtwitter, or vxtwitter URL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of X/Twitter URLs to scrape",
            },
        },
        "required": ["urls"],
    },
}

registry.register(
    name="x_scrape",
    toolset="web",
    schema=X_SCRAPE_SCHEMA,
    handler=_x_scrape_handler,
    description=X_SCRAPE_SCHEMA["description"],
)
