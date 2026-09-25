"""
web_search.py — the "web_search" tool (see main.py TOOL_DECLARATIONS).

Multi-backend web search with a "first to answer wins" race (_race, below)
across whichever of these are configured: Gemini's own grounded search,
DuckDuckGo (always available, no key needed), Google Programmable Search,
and Tavily — so a slow or quota-exhausted backend never blocks the answer,
and a backend that hedges/refuses instead of answering (_is_refusal) is
treated as a failure so a real DDG result can still win instead.

Five modes, each a thin wrapper around _race with different formatting:
  search  (_search)  — general web search
  news    (_news)    — latest headlines on a topic
  research(_research)— deeper, more comprehensive answer
  price   (_price)   — product price lookup
  compare (_compare) — side-by-side comparison of multiple items

web_search(...) is the entry point main.py's tool dispatcher calls; it
reads parameters["mode"] and routes to the matching function above.
"""
import json
import re
import sys
import threading
import time
from pathlib import Path

# Once Gemini reports a daily quota/rate-limit exhaustion (free tier = 20
# generate_content requests/day), further attempts will just fail the same
# way for a while. Rather than re-trying (and eating a small chunk of the
# race timeout) on every single search, remember it and skip straight to
# DDG until the cooldown passes.
_GEMINI_QUOTA_RE            = re.compile(r"RESOURCE_EXHAUSTED|quota", re.IGNORECASE)
_gemini_quota_exhausted_until = 0.0
_GEMINI_QUOTA_COOLDOWN_SECS   = 1800   # 30 minutes

# Phrases that indicate Gemini hedged/declined rather than actually answering.
# Without this check, a fast-arriving refusal could "win" the race against
# DDG simply by responding quickly, even though it contains no real answer —
# this was letting non-answers through for sensitive topics (e.g. news about
# a real person's death) instead of falling back to DDG's actual results.
_REFUSAL_RE = re.compile(
    r"\bi (can(not|'t)|am not able to|won'?t|don'?t have (enough|access to)|"
    r"cannot verify|do not have (information|details)|am unable to)\b"
    r"|i'?m sorry|i apologize|as an ai|cannot (provide|confirm)|"
    r"no (verified|reliable|confirmed) information|"
    r"i (do not|don't) have (real-?time|up-?to-?date) (information|data)",
    re.IGNORECASE,
)


def _is_refusal(text: str) -> bool:
    if not text:
        return False
    return bool(_REFUSAL_RE.search(text[:400]))

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _google_cse_configured() -> bool:
    cfg = _load_config()
    return bool(cfg.get("google_cse_api_key", "").strip() and cfg.get("google_cse_cx", "").strip())


def _google_cse_search(query: str, max_results: int = 6) -> list[dict]:
    """
    Google Programmable Search Engine (Custom Search JSON API) — 100 free
    queries/day, official Google product, separate quota from the Gemini
    generate_content API. Requires 'google_cse_api_key' and 'google_cse_cx'
    set in config/api_keys.json.
    """
    import requests

    cfg = _load_config()
    key = cfg.get("google_cse_api_key", "").strip()
    cx  = cfg.get("google_cse_cx", "").strip()
    if not key or not cx:
        raise RuntimeError("Google CSE not configured (missing google_cse_api_key / google_cse_cx).")

    resp = requests.get(
        "https://www.googleapis.com/customsearch/v1",
        params={"key": key, "cx": cx, "q": query, "num": min(max_results, 10)},
        timeout=8,
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("items", [])[:max_results]:
        results.append({
            "title":   item.get("title", ""),
            "snippet": item.get("snippet", ""),
            "url":     item.get("link", ""),
        })
    return results


def _tavily_configured() -> bool:
    cfg = _load_config()
    return bool(cfg.get("tavily_api_key", "").strip())


def _tavily_search(query: str, max_results: int = 6, topic: str = "general") -> list[dict]:
    """
    Tavily — search API built for AI agents. Free tier: 1,000 searches/month,
    no credit card required. Requires 'tavily_api_key' in config/api_keys.json.
    Returns Tavily's own LLM-generated answer (when available) as the first
    result, followed by individual source snippets.
    """
    import requests

    cfg = _load_config()
    key = cfg.get("tavily_api_key", "").strip()
    if not key:
        raise RuntimeError("Tavily not configured (missing tavily_api_key).")

    resp = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "query":          query,
            "max_results":    max_results,
            "topic":          topic if topic in ("general", "news", "finance") else "general",
            "include_answer": True,
            "search_depth":   "basic",
        },
        timeout=8,
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    answer = (data.get("answer") or "").strip()
    if answer:
        results.append({"title": "Summary", "snippet": answer, "url": ""})
    for item in data.get("results", [])[:max_results]:
        results.append({
            "title":   item.get("title", ""),
            "snippet": item.get("content", ""),
            "url":     item.get("url", ""),
        })
    return results


def _gemini_search(query: str) -> str:
    from google import genai

    client   = genai.Client(api_key=_get_api_key())
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=query,
        config={"tools": [{"google_search": {}}]},
    )

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title":   r.get("title",  ""),
                "snippet": r.get("body",   ""),
                "url":     r.get("href",   ""),
            })
    return results


def _ddg_news(query: str, max_results: int = 8) -> list[dict]:
    """DDG news search — returns actual articles, not website homepages."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("url",    ""),
                    "source":  r.get("source", ""),
                })
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG news() failed ({e}) — falling back to text search")
        results = _ddg_search(query, max_results=max_results)
    return results


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):   lines.append(f"{i}. {r['title']}")
        if r.get("snippet"): lines.append(f"   {r['snippet']}")
        if r.get("url"):     lines.append(f"   Source: {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_news(query: str, results: list[dict]) -> str:
    if not results:
        return f"No news found for: {query}"

    lines = [f"Latest news: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        if not title:
            continue
        src = f"  [{r['source']}]" if r.get("source") else ""
        lines.append(f"{i}. {title}{src}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:140]}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


# ── Briefing helper ────────────────────────────────────────────────────────────

def _gemini_headlines(n: int = 5) -> tuple[list[str], str]:
    """
    Fetches current headlines via Gemini grounded search.
    Optimised for speed: minimal prompt + strict token cap.
    Returns (headline_list, raw_text_for_display).
    """
    import re
    from google import genai

    client = genai.Client(api_key=_get_api_key())
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"Current world news: {n} headlines. Numbered list, titles only.",
        config={"tools": [{"google_search": {}}]},
    )

    raw = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            raw += part.text

    headlines = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Only accept lines that begin with a number — skips preamble/closing sentences
        if not re.match(r'^[\d]+[.\)\-]', line):
            continue
        clean = re.sub(r'^[\d]+[.\)\-]\s*', '', line)
        clean = re.sub(r'^\*+\s*',          '', clean).strip()
        if clean and len(clean) > 10:
            headlines.append(clean)

    return headlines[:n], raw.strip()


# ── Modes ──────────────────────────────────────────────────────────────────────

def _race(
    gemini_query:  str,
    ddg_query:     str,
    ddg_fn=None,
    format_fn=None,
    ddg_max:       int   = 6,
    timeout:       float = 8.0,
    min_len:       int   = 40,
    tavily_topic:  str   = "general",
) -> str | None:
    """
    Runs Gemini grounded search, a DDG search, and (if configured) Google
    Programmable Search Engine and/or Tavily, all in parallel, returning
    whichever delivers a valid, non-trivial result first. Bounded by
    `timeout` so a slow/hanging backend never stalls the caller. Returns
    None if every attempted backend failed to produce anything usable in time.
    """
    ddg_fn     = ddg_fn    or _ddg_search
    format_fn  = format_fn or _format_ddg
    use_google = _google_cse_configured()
    use_tavily = _tavily_configured()
    total_sources = 2 + int(use_google) + int(use_tavily)

    result_box = [None]   # first valid result lands here
    lock       = threading.Lock()
    done_evt   = threading.Event()
    failures   = [0]

    def _store(r: str) -> None:
        if r and len(r) > min_len:
            with lock:
                if result_box[0] is None:
                    result_box[0] = r
            done_evt.set()
        else:
            with lock:
                failures[0] += 1
                if failures[0] >= total_sources:   # every attempted source failed
                    done_evt.set()

    def _try_gemini():
        global _gemini_quota_exhausted_until

        if time.time() < _gemini_quota_exhausted_until:
            remaining = int(_gemini_quota_exhausted_until - time.time())
            print(f"[WebSearch] ⏭️  Skipping Gemini (quota exhausted, {remaining}s left in cooldown) — DDG only")
            _store("")
            return

        try:
            r = _gemini_search(gemini_query)
            if _is_refusal(r):
                print(f"[WebSearch] ⚠️ Gemini hedged/declined — waiting on DDG instead: {r[:100]!r}")
                _store("")
            else:
                _store(r)
        except Exception as e:
            if _GEMINI_QUOTA_RE.search(str(e)):
                _gemini_quota_exhausted_until = time.time() + _GEMINI_QUOTA_COOLDOWN_SECS
                print(f"[WebSearch] 🚫 Gemini daily quota exhausted — pausing Gemini search for "
                      f"{_GEMINI_QUOTA_COOLDOWN_SECS // 60} min, using DuckDuckGo only in the meantime.")
            else:
                print(f"[WebSearch] ⚠️ Gemini failed ({e})")
            _store("")

    def _try_ddg():
        try:
            results = ddg_fn(ddg_query, max_results=ddg_max)
            _store(format_fn(ddg_query, results))
        except Exception as e:
            print(f"[WebSearch] ⚠️ DDG failed ({e})")
            _store("")

    def _try_google():
        try:
            results = _google_cse_search(ddg_query, max_results=ddg_max)
            _store(format_fn(ddg_query, results))
        except Exception as e:
            print(f"[WebSearch] ⚠️ Google CSE failed ({e})")
            _store("")

    def _try_tavily():
        try:
            results = _tavily_search(ddg_query, max_results=ddg_max, topic=tavily_topic)
            _store(format_fn(ddg_query, results))
        except Exception as e:
            print(f"[WebSearch] ⚠️ Tavily failed ({e})")
            _store("")

    threading.Thread(target=_try_gemini, daemon=True).start()
    threading.Thread(target=_try_ddg,    daemon=True).start()
    if use_google:
        threading.Thread(target=_try_google, daemon=True).start()
    if use_tavily:
        threading.Thread(target=_try_tavily, daemon=True).start()

    done_evt.wait(timeout=timeout)
    return result_box[0]


def _search(query: str) -> str:
    """Default search — Gemini grounded and DDG race in parallel, first usable result wins."""
    result = _race(query, query, ddg_max=6, timeout=8.0)
    return result or f"No results found for: {query}"


_GENERIC_NEWS_TERMS = {
    "", "news", "top news", "latest news", "today's news",
    "world news", "current events", "headlines",
}


def _news(query: str) -> str:
    """
    Runs Gemini grounded search AND DDG news in parallel.
    Returns whichever delivers a valid result first; cancels the other.

    A vague/generic request (empty query, or phrases like "news"/"latest
    news") is treated as "give me today's top headlines" — a specific,
    well-formed ask — rather than passed through as-is, which produced
    inconsistent or self-referential queries like "latest news today: news".
    """
    is_general = query.strip().lower() in _GENERIC_NEWS_TERMS

    if is_general:
        gemini_query = (
            "What are today's top news headlines? List the 5 most important "
            "world news stories from today only, with a one-line summary of each."
        )
        ddg_query = "top news today"
    else:
        gemini_query = f"latest news today: {query}"
        ddg_query    = query

    result = _race(
        gemini_query, ddg_query,
        ddg_fn=_ddg_news, format_fn=_format_news,
        ddg_max=8, timeout=10.0, min_len=60,
        tavily_topic="news",
    )
    return result or f"No news found for: {query}"


def _research(query: str) -> str:
    """
    Deep dive — races Gemini (comprehensive explanation) against a wider DDG
    fetch, same consistent parallel pattern as search/news.
    """
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, and important nuances."
    )
    result = _race(research_query, query, ddg_max=10, timeout=9.0)
    return result or f"No results found for: {query}"


def _price(query: str) -> str:
    """Product price lookup — races Gemini against DDG for current market prices."""
    price_query = f"current price of {query} — how much does it cost today"
    result = _race(price_query, f"{query} price buy", ddg_max=6, timeout=8.0)
    return result or f"No results found for: {query}"


def _compare(items: list[str], aspect: str) -> str:
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )
    try:
        return _gemini_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini compare failed: {e} — falling back to DDG")

    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
        except Exception:
            all_results[item] = []

    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {item}")
        for r in all_results.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(f"  • {r['snippet']}")
            if r.get("url"):
                lines.append(f"    {r['url']}")
    return "\n".join(lines)


# ── Public entry point ─────────────────────────────────────────────────────────

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """Entry point called from main.py's tool dispatcher — routes on
    parameters["mode"] to _search/_news/_research/_price/_compare."""
    params = parameters or {}
    query  = params.get("query", "").strip()
    mode   = params.get("mode",  "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query."

    if items and mode not in ("compare",):
        mode = "compare"

    if player:
        player.write_log(f"[Search:{mode}] {query or ', '.join(items)}")

    print(f"[WebSearch] 🔍 mode={mode!r}  query={query!r}")

    try:
        if mode == "compare" and items:
            return _compare(items, aspect)
        if mode == "news":
            return _news(query)
        if mode == "research":
            return _research(query)
        if mode == "price":
            return _price(query)
        return _search(query)

    except Exception as e:
        print(f"[WebSearch] ❌ All backends failed: {e}")
        return f"Search failed: {e}"
