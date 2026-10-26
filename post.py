"""
Hexalogic LinkedIn autoposter v4

Pipeline per post:
  1. RESEARCH   pull fresh stories from live feeds (AI news, dev tools, mobile, Hacker News, GitHub, HF)
  2. EDITOR     LLM picks the story most likely to get developers talking
  3. FACTS      fetch the article text so the writer works from real, current facts
  4. WRITER     LLM drafts caption + 6 carousel slides as JSON
  5. CRITIC     LLM edits the draft (hook, voice, fact-check against FACTS)
  6. GATE       code checks banned phrases, fake "I tried it" claims, numbers that are not in FACTS
  7. RENDER     slides -> PDF carousel (falls back to multi-image, then single image, then text)
  8. POST       LinkedIn Posts API
"""
import os, sys, json, random, re, io, glob, time, html
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter

sys.stdout.reconfigure(line_buffering=True)

GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()
LI_TOKEN = os.environ.get("LINKEDIN_TOKEN", "").strip()
LI_PERSON = os.environ.get("LINKEDIN_PERSON_ID", "").strip()
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
# Set LINKEDIN_AUTHOR (urn:li:organization:<id>) to post on the company page. Empty = personal profile.
AUTHOR = os.environ.get("LINKEDIN_AUTHOR", "").strip() or f"urn:li:person:{LI_PERSON}"

UA = {"User-Agent": "Mozilla/5.0 (compatible; HexalogicBot/1.0; +https://hexalogictechandsolutions.com)"}

# ============================================================ formats
FORMATS = ["ai_news", "dev_tools", "versus", "story", "term", "mobile", "ai_news", "fullstack"]
LABELS = {"ai_news": "AI NEWS", "versus": "VERSUS", "term": "NEW TERM", "dev_tools": "DEV TOOLS",
          "mobile": "MOBILE DEV", "mobile_tip": "MOBILE DEV", "story": "STORY TIME", "fullstack": "FULL-STACK"}
POOL_OF = {"ai_news": "ai", "versus": "ai", "term": "ai", "dev_tools": "dev", "mobile": "mobile"}

EVERGREEN = {
    "fullstack": ["Next.js caching", "FastAPI vs Node.js", "Postgres indexing basics", "Docker for local dev",
                  "auth mistakes to avoid", "React Server Components", "API error handling",
                  "keeping secrets out of your repo", "database migrations without downtime",
                  "rate limiting an API", "image optimization on the web", "TypeScript strict mode"],
    "mobile_tip": ["Flutter vs React Native", "offline-first mobile apps", "app startup time",
                   "push notifications done right", "state management in Flutter",
                   "mobile app release checklist", "handling slow networks in apps",
                   "common app store rejection reasons"],
    "story": ["a deployment that broke on a Friday", "scope creep on a client project",
              "a slow page that turned out to be one bad query", "choosing MVP features",
              "a bug that only happened in production", "a rewrite that was not worth it",
              "estimating projects", "saying no to a feature", "client communication"],
}

FORMAT_TASKS = {
    "ai_news": "Explain this AI news for developers and founders: what happened, what is actually new, what it means for people building products. Include one honest caveat or limitation if FACTS contains one.",
    "versus": "Compare the two things in FACTS head to head. Use versus slides only for numbers that FACTS gives for both sides. Finish with who should pick which, based only on FACTS.",
    "term": "Explain this new term or concept in simple words for a smart friend who is not in tech: what it is, why it exists, a tiny real-world picture, and where it falls short.",
    "dev_tools": "Break down this developer tool, framework release or feature: what it does, who it helps, one limitation, and whether you would adopt it now or wait.",
    "mobile": "Break down this mobile development news or trend for app developers: what changed and what to do about it.",
    "mobile_tip": "Teach one practical mobile app tip as slides: the problem, the common mistake, the better way, a tiny example in plain words, the takeaway.",
    "story": "Tell a short story-time post in five beats: the situation, what went wrong, the fix, the lesson, the takeaway. Keep it general and relatable. No names, no numbers, no invented specifics. Frame it as a pattern seen on client projects.",
    "fullstack": "Teach one practical full-stack tip as slides: the problem, the common mistake, the better way, a tiny example in plain words, the takeaway.",
}

EDITOR_TASKS = {
    "ai_news": "Pick the ONE story that will make developers talk: a big model or product launch, a surprising number, a real shift. Prefer the last 7 days.",
    "versus": "Pick the story that compares two rival products or models head to head. If no single story does, pick the TWO stories about the two rivals. Up to 2 picks.",
    "term": "Pick the story that introduces a NEW term, feature name or concept developers will start hearing. Put that term in the 'term' field.",
    "dev_tools": "Pick the new tool, framework release or feature that working web or full-stack developers will most want to know about.",
    "mobile": "Pick the news or release that matters most to people building mobile apps.",
}

HOOK_STYLES = [
    "Open with a bold, slightly contrarian one-line opinion.",
    "Open by bluntly stating a common mistake developers make.",
    "Open by busting a common myth in one line.",
    "Open with a short honest admission like 'I used to...' (keep it general, no invented specifics).",
    "Open with a plain-words promise of a time saver or simple fix.",
]
NEWS_STYLES = [
    "Open with the single most surprising fact from FACTS, stated in plain words.",
    "Open with a punchy one-line take on why this matters for people who build software.",
]

BANNED = ["delve", "game-changer", "game changer", "game-changing", "unlock", "leverage", "elevate",
          "revolutionize", "landscape", "realm", "tapestry", "seamless", "robust", "cutting-edge",
          "fast-paced", "it's not just", "let's dive", "buckle up", "here's the thing",
          "at the end of the day", "thrilled", "excited to share", "ever wondered", "painless",
          "streamline", "empower", "in today's", "dive into", "supercharge", "next level"]
CLAIM_RE = re.compile(r"\b(?:I|we)(?:'ve| have)?\s+(?:just\s+)?(?:tried|used|tested|built|switched|started using|benchmarked)\b", re.I)

SCHEMA = ('{"caption":"hook line\\n\\nsecond line\\n\\nclosing question?",'
          '"hashtags":["Tag1","Tag2","Tag3","Tag4"],'
          '"slides":['
          '{"type":"cover","kicker":"BREAKING","title":"3 to 7 word headline","keyword":"one word from title"},'
          '{"type":"point","title":"short title","body":"one or two short sentences","keyword":""},'
          '{"type":"versus","title":"what is compared","left":{"name":"A","value":"53"},"right":{"name":"B","value":"53"},"note":"one short line"},'
          '{"type":"cta","title":"closing question for the audience","body":"Follow Hexalogic for more dev news"}'
          ']}')

WRITER_SYSTEM = (
    "You are the content lead for Hexalogic Tech Solutions, a small software house (web, mobile apps, AI, automation). "
    "You write LinkedIn carousel posts for developers and founders. They are specific, opinionated, easy to skim, "
    "and sound like a person, not a press release.\n\n"
    "HARD RULES\n"
    "- Use ONLY facts, names, numbers and dates that appear in the FACTS block. Your own memory of AI models, "
    "products and versions is out of date, so never add model names, version numbers, scores or prices from memory. "
    "If FACTS gives no number for something, do not invent one.\n"
    "- You have not personally used anything in FACTS. Never write 'I tried', 'I tested', 'I used', 'we built'. "
    "Write as someone who read about it and has an opinion.\n"
    "- If FACTS says '(none)', this is an evergreen post: keep stories general ('on a client project', no names) "
    "and use no number above 10.\n"
    "- Plain text. No markdown, no emoji, no em dashes. Contractions are fine.\n"
    "- Never use these words or phrases: " + ", ".join(BANNED) + ".\n\n"
    "CAPTION: line 1 is the hook: bold, specific, under 90 characters, not a question. Then 2 to 4 short lines, "
    "blank line between lines. Last line: one specific question that is easy to answer in a comment. "
    "250 to 450 characters total. No hashtags in the caption.\n\n"
    "SLIDES: exactly 6 slides: 1 cover, 4 content slides (type point or versus), 1 cta. "
    "Cover title: 3 to 7 words, plus a keyword that appears in the title. "
    "Point: title up to 8 words, body up to 22 words, one idea. "
    "Versus: left and right each have a short name (up to 3 words) and a value (up to 6 characters) and you may use it "
    "only if both values are in FACTS. cta: title is a question for the audience.\n\n"
    "Return ONLY JSON with this shape:\n" + SCHEMA
)

CRITIC_SYSTEM = (
    "You are a ruthless LinkedIn editor and fact-checker for a developer audience. You get FACTS, a draft post as JSON, "
    "and problems found by an automated checker. Return an improved version in the SAME JSON schema.\n"
    "Checklist:\n"
    "1. The hook stops the scroll and is specific. Rewrite it if it is bland.\n"
    "2. Every name, number, date and claim is supported by FACTS. Delete or rewrite anything that is not. "
    "Do not add facts from your own memory.\n"
    "3. No claim of personal use ('I tried', 'we built').\n"
    "4. Human voice, short lines, none of these words: " + ", ".join(BANNED) + ". No markdown, emoji or em dashes.\n"
    "5. Caption 250 to 450 characters, hook under 90 characters, last line is an easy question.\n"
    "6. Slide text is short enough to read on a phone. 6 slides: cover first, cta last.\n"
    "7. Fix every problem the checker listed.\n"
    "Return ONLY the JSON."
)


# ============================================================ text helpers
def clean(s):
    s = re.sub(r"```.*?```", "", s, flags=re.S)
    s = re.sub(r"\*+|`+", "", s)
    s = re.sub(r"^#+\s*", "", s, flags=re.M)
    s = re.sub(r"^\s*[-•]\s+", "", s, flags=re.M)
    s = s.replace(" — ", ", ").replace("—", ", ").replace("–", "-")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def strip_html(s):
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)
    except Exception:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ============================================================ research sources
ATOM = "{http://www.w3.org/2005/Atom}"
AI_RE = re.compile(r"\b(ai|llm|llms|gpt|claude|gemini|agent|agents|openai|anthropic|model|models|open source|deepseek|llama|qwen|mistral)\b", re.I)
DEV_RE = re.compile(r"\b(api|framework|rust|python|typescript|javascript|react|next\.?js|postgres|sql|database|docker|kubernetes|cli|compiler|release|launch|open source|library|sdk|browser|css|node|deno|bun|go|golang|swift|kotlin|flutter)\b", re.I)


def fetch_feed(url, src, days=14, limit=10):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for it in list(root.iter("item")) + list(root.iter(ATOM + "entry")):
        if it.tag == "item":
            title = it.findtext("title") or ""
            link = (it.findtext("link") or "").strip()
            desc = it.findtext("description") or it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""
            dt = parse_date(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"))
        else:
            title = it.findtext(ATOM + "title") or ""
            link = ""
            for l in it.findall(ATOM + "link"):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href", "")
                    break
            desc = it.findtext(ATOM + "summary") or it.findtext(ATOM + "content") or ""
            dt = parse_date(it.findtext(ATOM + "published") or it.findtext(ATOM + "updated"))
        if dt and dt < cutoff:
            continue
        title = strip_html(title)
        if not title or not link:
            continue
        out.append({"title": title, "url": link, "summary": strip_html(desc)[:400], "src": src, "dt": dt})
        if len(out) >= limit:
            break
    return out


def fetch_hn(filter_re):
    r = requests.get("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=60", headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for h in r.json().get("hits", []):
        t = h.get("title") or ""
        if not filter_re.search(t):
            continue
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        out.append({"title": t, "url": url, "summary": f"{h.get('points', 0)} points on Hacker News",
                    "src": "Hacker News", "dt": parse_date(h.get("created_at"))})
    return out[:10]


def fetch_devto(params):
    r = requests.get("https://dev.to/api/articles?" + params, headers=UA, timeout=20)
    r.raise_for_status()
    return [{"title": a["title"], "url": a["url"], "summary": (a.get("description") or "")[:300],
             "src": "dev.to", "dt": parse_date(a.get("published_at"))} for a in r.json()][:8]


def fetch_hf_trending():
    r = requests.get("https://huggingface.co/api/models?sort=trendingScore&limit=8", headers=UA, timeout=20)
    r.raise_for_status()
    return [{"title": f"Trending on Hugging Face: {m.get('id')}",
             "url": f"https://huggingface.co/{m.get('id')}",
             "summary": f"{m.get('pipeline_tag', 'model')}, {m.get('likes', 0)} likes, {m.get('downloads', 0)} downloads",
             "src": "Hugging Face", "dt": None} for m in r.json()]


def fetch_github_new():
    since = (date.today() - timedelta(days=7)).isoformat()
    headers = dict(UA)
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    r = requests.get(f"https://api.github.com/search/repositories?q=created:>{since}+stars:>150&sort=stars&order=desc&per_page=10",
                     headers=headers, timeout=20)
    r.raise_for_status()
    return [{"title": f"New on GitHub: {x['full_name']}", "url": x["html_url"],
             "summary": f"{x.get('description') or ''} ({x['stargazers_count']} stars, {x.get('language') or 'n/a'})",
             "src": "GitHub", "dt": parse_date(x.get("created_at"))} for x in r.json().get("items", [])]


def feed(name, url):
    return (name, lambda: fetch_feed(url, name))


SOURCES = {
    "ai": [
        feed("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
        feed("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        feed("Hugging Face blog", "https://huggingface.co/blog/feed.xml"),
        feed("Google AI blog", "https://blog.google/technology/ai/rss/"),
        feed("OpenAI news", "https://openai.com/news/rss.xml"),
        feed("Ars Technica AI", "https://arstechnica.com/ai/feed/"),
        feed("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
        ("Hacker News", lambda: fetch_hn(AI_RE)),
        ("Hugging Face trending", fetch_hf_trending),
    ],
    "dev": [
        feed("GitHub blog", "https://github.blog/feed/"),
        feed("Vercel blog", "https://vercel.com/atom"),
        feed("Next.js blog", "https://nextjs.org/feed.xml"),
        feed("Node.js blog", "https://nodejs.org/en/feed/blog.xml"),
        feed("React blog", "https://react.dev/rss.xml"),
        feed("web.dev", "https://web.dev/feed.xml"),
        ("dev.to top", lambda: fetch_devto("top=7&per_page=20")),
        ("Hacker News", lambda: fetch_hn(DEV_RE)),
        ("GitHub new repos", fetch_github_new),
    ],
    "mobile": [
        feed("Android Developers", "https://android-developers.googleblog.com/feeds/posts/default"),
        feed("React Native blog", "https://reactnative.dev/blog/rss.xml"),
        feed("Apple Developer news", "https://developer.apple.com/news/rss/news.rss"),
        feed("Flutter blog", "https://blog.flutter.dev/feed"),
        ("dev.to Flutter", lambda: fetch_devto("tag=flutter&top=7&per_page=10")),
        ("dev.to React Native", lambda: fetch_devto("tag=reactnative&top=7&per_page=10")),
        ("dev.to Android", lambda: fetch_devto("tag=android&top=7&per_page=10")),
        ("dev.to iOS", lambda: fetch_devto("tag=ios&top=7&per_page=10")),
    ],
}


def gather(pool, recent):
    def run(src):
        name, fn = src
        try:
            res = fn()
            print(f"  source {name}: {len(res)} items")
            return res
        except Exception as e:
            print(f"  source {name} failed: {str(e)[:90]}")
            return []

    items = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(run, SOURCES[pool]):
            items += res
    seen, out = set(), []
    recent_l = {r.lower() for r in recent}
    for it in items:
        key = it["title"].lower()
        if key in seen or key in recent_l or it["url"] in recent_l:
            continue
        seen.add(key)
        out.append(it)
    out.sort(key=lambda x: x["dt"].timestamp() if x["dt"] else 0, reverse=True)
    return out[:30]


def article_text(url, limit=3500):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        if "html" not in r.headers.get("content-type", "html"):
            return ""
        paras = re.findall(r"<p[^>]*>(.*?)</p>", r.text, flags=re.S | re.I)
        paras = [strip_html(p) for p in paras]
        text = " ".join(p for p in paras if len(p) > 60)
        return text[:limit]
    except Exception as e:
        print("  article fetch failed:", url[:70], str(e)[:70])
        return ""


def build_facts(picked):
    blocks = []
    for n, c in enumerate(picked, 1):
        body = article_text(c["url"])
        if len(body) < 300:
            body = (body + " " + c["summary"]).strip()
        when = c["dt"].date().isoformat() if c["dt"] else "date unknown"
        blocks.append(f"SOURCE {n} ({c['src']}, {when}): {c['title']}\nURL: {c['url']}\n{body}")
    return "\n\n".join(blocks)[:9000]


# ============================================================ Groq
GROQ_HEADERS = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
PREFERRED = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "llama-3.1-8b-instant", "openai/gpt-oss-20b"]
_MODELS = None
TOKENS = 0


def groq_models():
    global _MODELS
    if _MODELS is not None:
        return _MODELS
    models = list(PREFERRED)
    try:
        mr = requests.get("https://api.groq.com/openai/v1/models", headers=GROQ_HEADERS, timeout=30)
        if mr.ok:
            available = [m["id"] for m in mr.json().get("data", [])]
            skip = ("whisper", "guard", "tts", "playai", "embed", "orpheus", "safeguard", "compound")
            chat = [m for m in available if not any(s in m.lower() for s in skip)]
            models = [m for m in PREFERRED if m in chat] + [m for m in chat if m not in PREFERRED]
        else:
            print("Could not list Groq models:", mr.status_code, mr.text[:200])
    except Exception as e:
        print("Model list failed:", e)
    _MODELS = models[:6]
    return _MODELS


def groq_once(model, messages, max_tokens, temperature):
    global TOKENS
    body = {"model": model, "temperature": temperature, "max_completion_tokens": max_tokens, "messages": messages}
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
        body["max_completion_tokens"] = max_tokens + 2000
    for attempt in range(2):
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                              headers=GROQ_HEADERS, json=body, timeout=120)
        except Exception as e:
            print("  request error:", model, e)
            return None
        if r.status_code == 429 and attempt == 0:
            try:
                wait = min(float(r.headers.get("retry-after", 20)), 60)
            except ValueError:
                wait = 20
            print(f"  rate limited on {model}, waiting {wait:.0f}s")
            time.sleep(wait + 1)
            continue
        if not r.ok:
            print("  model failed:", model, r.status_code, r.text[:200])
            return None
        j = r.json()
        TOKENS += (j.get("usage") or {}).get("total_tokens", 0)
        return (j["choices"][0]["message"].get("content") or "").strip()
    return None


def extract_json(txt):
    txt = re.sub(r"```(?:json)?", "", txt or "")
    a, b = txt.find("{"), txt.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(txt[a:b + 1])
    except Exception:
        return None


def llm_json(messages, max_tokens=3000, temperature=0.7, check=None):
    for model in groq_models():
        txt = groq_once(model, messages, max_tokens, temperature)
        if not txt:
            continue
        data = extract_json(txt)
        if data is None:
            print("  not valid JSON, trying next:", model)
            continue
        if check:
            err = check(data)
            if err:
                print("  rejected:", err, "|", model)
                continue
        print("  model:", model)
        return data
    raise RuntimeError("All Groq models failed, see log above")


# ============================================================ story picking + writing
def pick_story(fmt, cands):
    lines = [f"{k}. [{c['src']}] {c['title']} :: {c['summary'][:180]}" for k, c in enumerate(cands)]
    system = ("You are the editor of a developer-focused LinkedIn page. " + EDITOR_TASKS[fmt] +
              ' Return ONLY JSON: {"picks":[index,...],"term":"","angle":"one sentence on the most interesting angle"}')

    def ok(d):
        p = d.get("picks")
        if isinstance(p, list) and p and all(isinstance(x, int) and 0 <= x < len(cands) for x in p):
            return None
        return "bad picks"

    try:
        d = llm_json([{"role": "system", "content": system},
                      {"role": "user", "content": "Candidates:\n" + "\n".join(lines)}],
                     max_tokens=500, temperature=0.3, check=ok)
        picks = list(dict.fromkeys(d["picks"]))[:2 if fmt == "versus" else 1]
        return [cands[k] for k in picks], str(d.get("term") or ""), str(d.get("angle") or "")
    except Exception as e:
        print("  editor failed, using newest story:", e)
        return [cands[0]], "", ""


def S(x, n=300):
    return clean(str(x or ""))[:n]


def normalize(plan):
    cap = clean(str(plan.get("caption", "")))
    tags = [re.sub(r"[^A-Za-z0-9]", "", str(t)) for t in (plan.get("hashtags") or [])]
    tags = [t for t in tags if t][:4]
    slides = []
    for s in plan.get("slides") or []:
        if not isinstance(s, dict):
            continue
        t = str(s.get("type", "point")).lower()
        if t not in ("cover", "point", "versus", "cta"):
            t = "point"
        item = {"type": t, "title": S(s.get("title"), 90), "body": S(s.get("body"), 200),
                "keyword": S(s.get("keyword"), 30), "kicker": S(s.get("kicker"), 24), "note": S(s.get("note"), 140)}
        for side in ("left", "right"):
            v = s.get(side) if isinstance(s.get(side), dict) else {}
            item[side] = {"name": S(v.get("name"), 24), "value": S(v.get("value"), 14)}
        if t == "versus" and not all(item[x]["name"] and item[x]["value"] for x in ("left", "right")):
            item["type"] = "point"
            item["body"] = item["body"] or item["note"]
        slides.append(item)
    return {"caption": cap, "hashtags": tags, "slides": slides}


def schema_check(d):
    if not isinstance(d.get("caption"), str) or not isinstance(d.get("slides"), list) or len(d["slides"]) < 4:
        return "missing caption or slides"
    return None


NUM_RE = re.compile(r"[$€£]?\d+(?:[.,]\d+)*%?")


def num_core(tok):
    return re.sub(r"[^\d.]", "", tok).strip(".")


def all_text(plan):
    parts = [plan["caption"]]
    for s in plan["slides"]:
        parts += [s["title"], s["body"], s["note"], s["kicker"], s["left"]["name"], s["left"]["value"],
                  s["right"]["name"], s["right"]["value"]]
    return "\n".join(p for p in parts if p)


def check_plan(plan, facts):
    problems = []
    cap = plan["caption"]
    if not (150 <= len(cap) <= 650):
        problems.append(f"caption is {len(cap)} characters, needs 250 to 450")
    first = cap.split("\n")[0] if cap else ""
    if len(first) > 100:
        problems.append("hook line is longer than 90 characters")
    last = [l for l in cap.split("\n") if l.strip()][-1:] or [""]
    if "?" not in last[0]:
        problems.append("caption must end with a question")
    if len(plan["hashtags"]) < 2:
        problems.append("need at least 2 hashtags")
    sl = plan["slides"]
    if not (5 <= len(sl) <= 8):
        problems.append(f"{len(sl)} slides, need 6")
    elif sl[0]["type"] != "cover" or sl[-1]["type"] != "cta":
        problems.append("first slide must be type cover and last must be type cta")
    for k, s in enumerate(sl, 1):
        if len(s["title"].split()) > 10:
            problems.append(f"slide {k} title too long")
        if len(s["body"].split()) > 30:
            problems.append(f"slide {k} body too long")
    text = all_text(plan)
    low = text.lower()
    for b in BANNED:
        if b in low:
            problems.append(f"banned phrase: {b}")
    if CLAIM_RE.search(text):
        problems.append("claims personal use ('I tried', 'we built')")
    facts_nums = {num_core(t) for t in NUM_RE.findall(facts)}
    for tok in set(NUM_RE.findall(text)):
        core = num_core(tok)
        if not core:
            continue
        small = core.isdigit() and int(core) <= 10 and not tok.endswith("%") and tok[0] not in "$€£"
        if small:
            continue
        if core not in facts_nums:
            problems.append(f"number {tok} is not in FACTS")
    return problems


def write_plan(user_msg, facts):
    draft = llm_json([{"role": "system", "content": WRITER_SYSTEM}, {"role": "user", "content": user_msg}],
                     max_tokens=3000, temperature=0.85, check=schema_check)
    plan = normalize(draft)
    problems = check_plan(plan, facts)
    print("Draft problems:", problems or "none")
    for rnd in (1, 2):
        time.sleep(4)
        crit = (f"FACTS:\n{facts or '(none)'}\n\nAUTOMATED CHECKER PROBLEMS:\n"
                f"{chr(10).join(problems) if problems else 'none, but still sharpen the hook and the voice'}\n\n"
                f"DRAFT JSON:\n{json.dumps(plan)}")
        fixed = llm_json([{"role": "system", "content": CRITIC_SYSTEM}, {"role": "user", "content": crit}],
                         max_tokens=3000, temperature=0.5, check=schema_check)
        plan = normalize(fixed)
        problems = check_plan(plan, facts)
        print(f"After critic round {rnd}:", problems or "clean")
        if not problems:
            return plan
    raise RuntimeError("Quality gate failed: " + "; ".join(problems))


# ============================================================ slide rendering
SW, SH, M = 1080, 1350, 80
SANS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"]
REG = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
       "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
       "/usr/share/fonts/truetype/freefont/FreeSans.ttf"]
MONO = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"]


def _font(paths, size):
    for p in paths:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def font(size):
    return _font(SANS, size)


def font_reg(size):
    return _font(REG, size)


def mono(size):
    return _font(MONO, size)


_LOGO = "unset"


def load_logo():
    global _LOGO
    if _LOGO != "unset":
        return _LOGO
    _LOGO = None
    for p in sorted(glob.glob("logo*")) + sorted(glob.glob("Logo*")):
        try:
            _LOGO = Image.open(p).convert("RGBA")
            break
        except Exception as e:
            print("Could not open", p, e)
    if _LOGO is None:
        print("WARNING: no logo file found in repo root (expected logo.png)")
    return _LOGO


def wrap(d, text, f, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit(d, text, getfont, maxw, maxh, start=96, stop=44, maxlines=4, spacing=1.2):
    size = start
    while True:
        f = getfont(size)
        lines = wrap(d, text, f, maxw)
        if (len(lines) <= maxlines and len(lines) * size * spacing <= maxh) or size <= stop:
            return f, size, lines
        size -= 4


def pick_kw(headline, kw):
    words = [re.sub(r"[^A-Za-z0-9+#.]", "", w) for w in headline.split()]
    words = [w for w in words if w]
    if kw and kw.lower() in [w.lower() for w in words]:
        return kw
    return max(words, key=len) if words else ""


def draw_lines(d, lines, f, x, y, lh, fg, kw=None, kw_fg=None, kw_box=None):
    space = d.textlength(" ", font=f)
    kwl = (kw or "").lower()
    for ln in lines:
        cx = x
        for w in ln.split():
            core = re.sub(r"[^A-Za-z0-9+#.]", "", w).lower()
            wl = d.textlength(w, font=f)
            if kwl and core == kwl and kw_fg:
                if kw_box:
                    d.rectangle((cx - 10, y + 6, cx + wl + 10, y + lh - 4), fill=kw_box)
                d.text((cx, y), w, font=f, fill=kw_fg)
            else:
                d.text((cx, y), w, font=f, fill=fg)
            cx += wl + space
        y += lh
    return y


def theme(idx):
    k = idx % 3
    if k == 0:
        return dict(name="terminal", bg=(13, 17, 23), fg=(230, 237, 243), sub=(139, 148, 158), accent=(255, 189, 46),
                    num=(63, 185, 80), val=(255, 189, 46), kw_box=None, kw_fg=(255, 189, 46), panel=(33, 39, 48),
                    panel_fg=(230, 237, 243), chip_fill=None, chip_fg=(63, 185, 80), chip_line=(63, 185, 80))
    if k == 1:
        bg = [(250, 204, 21), (163, 230, 53), (251, 146, 60), (125, 211, 252)][(idx // 3) % 4]
        ink = (17, 17, 17)
        return dict(name="bold", bg=bg, fg=ink, sub=(55, 55, 55), accent=ink, num=ink, val=bg, kw_box=ink, kw_fg=bg,
                    panel=ink, panel_fg=bg, chip_fill=ink, chip_fg=bg, chip_line=None)
    acc = [(56, 189, 248), (244, 114, 182), (52, 211, 153), (251, 191, 36)][(idx // 3) % 4]
    return dict(name="grid", bg=(11, 15, 30), fg=(255, 255, 255), sub=(148, 163, 184), accent=acc, num=acc, val=acc,
                kw_box=None, kw_fg=acc, panel=(21, 30, 58), panel_fg=(255, 255, 255), chip_fill=None, chip_fg=acc,
                chip_line=acc)


def base_slide(th, label, page, total):
    img = Image.new("RGB", (SW, SH), th["bg"])
    d = ImageDraw.Draw(img)
    if th["name"] == "terminal":
        d.rounded_rectangle((28, 28, SW - 28, SH - 28), radius=26, fill=(22, 27, 34), outline=(48, 54, 61), width=2)
        for k, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse((64 + k * 34, 56, 84 + k * 34, 76), fill=c)
    elif th["name"] == "grid":
        for x in range(0, SW, 60):
            d.line([(x, 0), (x, SH)], fill=(20, 26, 48))
        for y in range(0, SH, 60):
            d.line([(0, y), (SW, y)], fill=(20, 26, 48))
        glow = Image.new("RGBA", (SW, SH), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse((SW - 520, -300, SW + 200, 420), fill=th["accent"] + (110,))
        glow = glow.filter(ImageFilter.GaussianBlur(110))
        img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
        d = ImageDraw.Draw(img)
    lf = font(26)
    lw = d.textlength(label, font=lf)
    y = 108
    if th["chip_fill"]:
        d.rounded_rectangle((M, y, M + lw + 44, y + 52), radius=26, fill=th["chip_fill"])
    else:
        d.rounded_rectangle((M, y, M + lw + 44, y + 52), radius=26, outline=th["chip_line"], width=3)
    d.text((M + 22, y + 11), label, font=lf, fill=th["chip_fg"])
    pg = f"{page:02d}/{total:02d}"
    d.text((SW - M - d.textlength(pg, font=mono(26)), y + 12), pg, font=mono(26), fill=th["sub"])
    return img, d


def footer(img, d, th, base=SH - 64):
    x = M
    logo = load_logo()
    if logo:
        d.rounded_rectangle((x, base - 72, x + 72, base), radius=14, fill="white")
        lg = logo.copy()
        lg.thumbnail((60, 60))
        img.paste(lg, (x + 6 + (60 - lg.width) // 2, base - 72 + 6 + (60 - lg.height) // 2), lg)
        x += 92
    d.text((x, base - 70), "Hexalogic Tech Solutions", font=font(28), fill=th["fg"])
    d.text((x, base - 32), "hexalogictechandsolutions.com", font=font(22), fill=th["sub"])


def slide_cover(th, label, s, page, total):
    img, d = base_slide(th, label, page, total)
    d.text((M, 330), (s["kicker"] or label).upper(), font=mono(34), fill=th["num"])
    d.rectangle((M, 388, M + 140, 398), fill=th["accent"])
    f, size, lines = fit(d, s["title"], font, SW - 2 * M, 560, start=150, stop=64, maxlines=5, spacing=1.18)
    draw_lines(d, lines, f, M, 440, int(size * 1.18), th["fg"], pick_kw(s["title"], s["keyword"]), th["kw_fg"], th["kw_box"])
    d.text((M, SH - 230), "Swipe  \u2192", font=font(40), fill=th["num"])
    footer(img, d, th)
    return img


def slide_point(th, label, s, n, page, total):
    img, d = base_slide(th, label, page, total)
    d.text((M, 250), f"{n:02d}", font=mono(160), fill=th["num"])
    f, size, lines = fit(d, s["title"], font, SW - 2 * M, 360, start=92, stop=56, maxlines=3)
    lh = int(size * 1.2)
    y = draw_lines(d, lines, f, M, 450, lh, th["fg"], s["keyword"], th["kw_fg"], th["kw_box"])
    y += 36
    bf = font_reg(54)
    for ln in wrap(d, s["body"], bf, SW - 2 * M)[:7]:
        if y > SH - 230:
            break
        d.text((M, y), ln, font=bf, fill=th["fg"])
        y += 80
    footer(img, d, th)
    return img


def slide_versus(th, label, s, page, total):
    img, d = base_slide(th, label, page, total)
    f, size, lines = fit(d, s["title"], font, SW - 2 * M, 220, start=72, stop=48, maxlines=3)
    y = draw_lines(d, lines, f, M, 210, int(size * 1.2), th["fg"], s["keyword"], th["kw_fg"], th["kw_box"])
    top, h, pw = max(y + 60, 460), 470, 390
    for k, side in enumerate(("left", "right")):
        x = M if k == 0 else SW - M - pw
        d.rounded_rectangle((x, top, x + pw, top + h), radius=28, fill=th["panel"])
        nf, ns, nl = fit(d, s[side]["name"], font, pw - 60, 120, start=44, stop=28, maxlines=2)
        draw_lines(d, nl, nf, x + 30, top + 40, int(ns * 1.2), th["panel_fg"])
        vf, vs, vl = fit(d, s[side]["value"], font, pw - 70, 200, start=110, stop=40, maxlines=1)
        d.text((x + 30, top + h // 2 - vs // 2), vl[0] if vl else "", font=vf, fill=th["val"])
    cx, cy = SW // 2, top + h // 2
    d.ellipse((cx - 48, cy - 48, cx + 48, cy + 48), fill=th["bg"], outline=th["accent"], width=4)
    vsf = font(34)
    d.text((cx - d.textlength("VS", font=vsf) / 2, cy - 20), "VS", font=vsf, fill=th["fg"])
    nfnt = font_reg(46)
    yy = top + h + 50
    for ln in wrap(d, s["note"], nfnt, SW - 2 * M)[:3]:
        d.text((M, yy), ln, font=nfnt, fill=th["fg"])
        yy += 68
    footer(img, d, th)
    return img


def slide_cta(th, label, s, page, total):
    img, d = base_slide(th, label, page, total)
    f, size, lines = fit(d, s["title"], font, SW - 2 * M, 520, start=100, stop=56, maxlines=5)
    y = draw_lines(d, lines, f, M, 360, int(size * 1.2), th["fg"], s["keyword"], th["kw_fg"], th["kw_box"])
    y += 50
    bf = font_reg(50)
    for ln in wrap(d, s["body"] or "Follow Hexalogic for more dev news", bf, SW - 2 * M)[:3]:
        d.text((M, y), ln, font=bf, fill=th["num"])
        y += 74
    footer(img, d, th)
    return img


def render_slides(plan, label, idx):
    th = theme(idx)
    total = len(plan["slides"])
    out, n = [], 0
    for page, s in enumerate(plan["slides"], 1):
        t = s["type"]
        if t == "cover":
            out.append(slide_cover(th, label, s, page, total))
        elif t == "versus":
            out.append(slide_versus(th, label, s, page, total))
        elif t == "cta":
            out.append(slide_cta(th, label, s, page, total))
        else:
            n += 1
            out.append(slide_point(th, label, s, n, page, total))
    return out


def to_png(img):
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def to_pdf(imgs):
    b = io.BytesIO()
    imgs[0].save(b, "PDF", save_all=True, append_images=imgs[1:], resolution=100.0)
    return b.getvalue()


# ============================================================ LinkedIn
def li_escape(s):
    for ch in ['\\', '|', '{', '}', '@', '[', ']', '(', ')', '<', '>', '#', '*', '_', '~']:
        s = s.replace(ch, '\\' + ch)
    return s


def candidate_versions(n=14):
    y, m = date.today().year, date.today().month
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


VERSION = None


def li(method, url, **kw):
    """LinkedIn call that finds a working API version (tries current month backwards on 426)."""
    global VERSION
    extra = kw.pop("headers", {})
    for ver in ([VERSION] if VERSION else candidate_versions()):
        headers = {"Authorization": f"Bearer {LI_TOKEN}", "LinkedIn-Version": ver,
                   "X-Restli-Protocol-Version": "2.0.0", **extra}
        r = requests.request(method, url, headers=headers, timeout=90, **kw)
        if r.status_code == 426:
            print("LinkedIn version not active, trying older:", ver)
            continue
        if r.status_code == 401:
            print("::error::LinkedIn token expired or invalid. Create a new one in the OAuth Token Generator "
                  "and update the LINKEDIN_TOKEN secret.")
            raise SystemExit(1)
        VERSION = ver
        return r
    raise SystemExit("No active LinkedIn API version found, check LinkedIn's docs")


def upload(kind, data):
    """kind = 'images' or 'documents'. Returns the URN."""
    r = li("POST", f"https://api.linkedin.com/rest/{kind}?action=initializeUpload",
           json={"initializeUploadRequest": {"owner": AUTHOR}})
    if not r.ok:
        raise RuntimeError(f"{kind} initializeUpload {r.status_code} {r.text[:300]}")
    v = r.json()["value"]
    u = requests.put(v["uploadUrl"], data=data, timeout=180,
                     headers={"Authorization": f"Bearer {LI_TOKEN}", "Content-Type": "application/octet-stream"})
    if not u.ok:
        raise RuntimeError(f"{kind} upload {u.status_code} {u.text[:300]}")
    return v["image"] if kind == "images" else v["document"]


def try_post(payload, label):
    p = li("POST", "https://api.linkedin.com/rest/posts", json=payload)
    if p.ok:
        print(f"Posted as {label} (LinkedIn version {VERSION})")
        return True
    print(f"Post as {label} failed: {p.status_code} {p.text[:300]}")
    return False


def publish(commentary, plan, imgs):
    base = {
        "author": AUTHOR, "commentary": commentary, "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False,
    }
    title = plan["slides"][0]["title"][:200]
    alts = [(s["title"] or title)[:200] for s in plan["slides"]]
    try:
        doc = upload("documents", to_pdf(imgs))
        if try_post({**base, "content": {"media": {"title": title, "id": doc}}}, "PDF carousel"):
            return
    except Exception as e:
        print("PDF carousel failed:", e)
    try:
        urns = [upload("images", to_png(im)) for im in imgs[:6]]
        content = {"multiImage": {"images": [{"id": u, "altText": alts[k]} for k, u in enumerate(urns)]}}
        if try_post({**base, "content": content}, "multi-image"):
            return
    except Exception as e:
        print("Multi-image failed:", e)
    try:
        urn = upload("images", to_png(imgs[0]))
        if try_post({**base, "content": {"media": {"title": title, "altText": title, "id": urn}}}, "single image"):
            return
    except Exception as e:
        print("Single image failed:", e)
    if try_post(base, "text only"):
        return
    raise SystemExit("Could not post in any format, see errors above")


# ============================================================ main
def save_state(state, i, recent, new_items):
    state["n"] = i + 1
    state["recent"] = (recent + [x for x in new_items if x])[-40:]
    json.dump(state, open("state.json", "w"))


def main():
    for k in ("GROQ_API_KEY", "LINKEDIN_TOKEN", "LINKEDIN_PERSON_ID"):
        if not os.environ.get(k, "").strip():
            raise SystemExit(f"Missing secret: {k}")
    tdate = os.environ.get("LINKEDIN_TOKEN_DATE", "").strip()
    if tdate:
        try:
            age = (date.today() - date.fromisoformat(tdate)).days
            if age >= 50:
                print(f"::warning::LinkedIn token is {age} days old and expires at about 60 days. Renew it soon.")
        except ValueError:
            print("LINKEDIN_TOKEN_DATE must look like 2026-10-26")

    state = json.load(open("state.json"))
    i = state["n"]
    recent = state.get("recent", [])
    fmt = FORMATS[i % len(FORMATS)]
    print(f"=== Post #{i + 1}, format: {fmt} ===")

    facts, picked, angle, term, topic = "", [], "", "", ""
    pool = POOL_OF.get(fmt)
    if pool:
        print("Researching...")
        cands = gather(pool, recent)
        print(f"{len(cands)} fresh candidates")
        if len(cands) >= 3:
            picked, term, angle = pick_story(fmt, cands)
            print("Picked:", " | ".join(c["title"] for c in picked))
            facts = build_facts(picked)
    eff = fmt
    if not facts:
        eff = "mobile_tip" if fmt == "mobile" else fmt if fmt in ("story", "fullstack") else "fullstack"
        pool_topics = [t for t in EVERGREEN[eff] if t not in recent] or EVERGREEN[eff]
        topic = random.choice(pool_topics)
        print(f"No research available, evergreen post on: {topic}")

    style = random.choice(NEWS_STYLES) if facts else HOOK_STYLES[i % len(HOOK_STYLES)]
    user_msg = (f"FORMAT: {eff}\nTASK: {FORMAT_TASKS[eff]}\nHOOK STYLE: {style}\n"
                f"ANGLE: {angle or 'n/a'}\nTOPIC OR TERM: {topic or term or 'see FACTS'}\n"
                f"FACTS:\n{facts or '(none)'}")
    try:
        plan = write_plan(user_msg, facts)
    except Exception:
        # do not get stuck on one slot forever: move on, then fail loudly
        save_state(state, i, recent, [c["title"] for c in picked] + [topic])
        raise
    print("Caption:\n" + plan["caption"])

    label = LABELS[eff]
    imgs = render_slides(plan, label, i)
    commentary = li_escape(plan["caption"])
    if plan["hashtags"]:
        commentary += "\n\n" + " ".join("{hashtag|\\#|%s}" % t for t in plan["hashtags"])
    publish(commentary, plan, imgs)

    save_state(state, i, recent, [c["title"] for c in picked] + [topic])
    print(f"Done. Groq tokens used this run: {TOKENS}")


if __name__ == "__main__":
    main()
