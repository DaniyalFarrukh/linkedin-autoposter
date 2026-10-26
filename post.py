import os, sys, json, random, re, io, requests
from datetime import date
from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(line_buffering=True)

GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()
LI_TOKEN = os.environ.get("LINKEDIN_TOKEN", "").strip()
LI_PERSON = os.environ.get("LINKEDIN_PERSON_ID", "").strip()
# Set LINKEDIN_AUTHOR (urn:li:organization:<id>) to post on the company page.
# Empty = post on your personal profile.
AUTHOR = os.environ.get("LINKEDIN_AUTHOR", "").strip() or f"urn:li:person:{LI_PERSON}"

KINDS = ["fullstack_tip", "ai_tool", "mobile_tip", "dev_story", "ai_tool", "new_tech", "lesson"]
LABELS = {
    "fullstack_tip": "FULL-STACK",
    "ai_tool": "AI NEWS",
    "mobile_tip": "MOBILE DEV",
    "dev_story": "DEV NOTES",
    "new_tech": "NEW TECH",
    "lesson": "LESSONS",
}

TOPICS = {
    "fullstack_tip": ["Next.js caching", "FastAPI vs Node.js", "Postgres indexing basics",
                      "Docker for local dev", "auth mistakes to avoid", "React Server Components",
                      "API error handling", "keeping secrets out of your repo",
                      "database migrations without downtime", "rate limiting an API",
                      "image optimization on the web", "TypeScript strict mode"],
    "mobile_tip": ["Flutter vs React Native", "offline-first mobile apps", "app startup time",
                   "push notifications done right", "state management in Flutter",
                   "mobile app release checklist", "handling slow networks in apps",
                   "common app store rejection reasons"],
    "new_tech": ["what AI coding assistants are good and bad at", "running small AI models locally",
                 "AI agents for business automation", "RAG vs fine-tuning",
                 "WebAssembly for web apps", "edge functions vs servers",
                 "vibe coding and where it breaks", "using AI to write tests"],
    "dev_story": ["a deployment that broke on a Friday", "scope creep on a client project",
                  "a slow page that turned out to be one bad query", "choosing MVP features",
                  "a bug that only happened in production", "a rewrite that was not worth it"],
    "lesson": ["estimating projects", "client communication", "code review habits",
               "technical debt", "saying no to features", "writing things down"],
}

TASKS = {
    "fullstack_tip": "Share one practical tip from building web apps. Give one concrete example in plain words (a tiny code idea written inline is fine, no code blocks).",
    "mobile_tip": "Share one practical tip from building mobile apps. Give one concrete example in plain words.",
    "new_tech": "Give an honest, balanced opinion on this topic from a working developer's view: what it is good for and where it falls short. No hype.",
    "dev_story": "Tell a short relatable story about this (keep it general, no names or numbers) and the lesson you took from it.",
    "lesson": "Share one lesson learned from building software for clients.",
    "ai_tool": "From these recent items pick the ONE most useful for developers and explain why it matters in practice. Only use facts present in the items. Do not include links.",
}

BANNED = ["delve", "game-changer", "game changer", "game-changing", "unlock", "leverage", "elevate",
          "revolutionize", "landscape", "realm", "tapestry", "seamless", "robust", "cutting-edge",
          "fast-paced", "it's not just", "let's dive", "buckle up", "here's the thing",
          "at the end of the day", "thrilled", "excited to share", "ever wondered"]

SYSTEM = (
    "You ghostwrite LinkedIn posts for a developer at Hexalogic Tech Solutions, a small software house "
    "(web, mobile apps, AI, automation). The post must read like a real developer typed it on a normal day, "
    "not like an AI wrote it.\n\n"
    "Voice rules:\n"
    "- First person, casual, direct. Contractions are fine. Mix short and longer sentences. Fragments are ok.\n"
    "- Open with a specific, concrete first line (a situation, a mistake, a surprising fact). "
    "Never open with a question and never open with 'In today's'.\n"
    "- Plain text only. No markdown, no asterisks, no bullet symbols, no headings, no em dashes. "
    "Blank line between short paragraphs (1 to 3 sentences each).\n"
    "- At most 1 emoji, or none.\n"
    "- Never use these words or phrases: " + ", ".join(BANNED) + ".\n"
    "- Use the 'it is not X, it is Y' contrast at most once.\n"
    "- Never invent statistics, client names, results or product features. If you mention experience, "
    "keep it general, like 'on a recent project'.\n"
    "- SHORT and to the point: 250 to 450 characters total, 3 to 5 short lines. One idea only, no setup or "
    "background. End with one specific question that is easy to answer in a comment.\n\n"
    "Output format, exactly:\n"
    "HEADLINE: <5 to 8 words for an image card, no punctuation at the end>\n"
    "POST:\n"
    "<post text, no hashtags inside it>\n"
    "HASHTAGS: <4 hashtags separated by commas, no # symbol, CamelCase, for example NextJS, WebDevelopment>"
)


# ---------- text cleanup ----------
def clean(s):
    s = re.sub(r"```.*?```", "", s, flags=re.S)
    s = re.sub(r"\*+|`+", "", s)
    s = re.sub(r"^#+\s*", "", s, flags=re.M)
    s = re.sub(r"^\s*[-•]\s+", "", s, flags=re.M)
    s = s.replace(" — ", ", ").replace("—", ", ").replace("–", "-")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def parse(raw):
    m = re.search(r"HEADLINE:\s*(.+?)\s*\n+POST:\s*(.+?)\s*\n+HASHTAGS:\s*(.+)$", raw, re.S | re.I)
    if not m:
        return None
    headline, post, tags = m.groups()
    headline = clean(headline).strip('"').strip()
    post = clean(post)
    tags = [re.sub(r"[^A-Za-z0-9]", "", t) for t in re.split(r"[,\n]", tags)]
    tags = [t for t in tags if t][:4]
    return headline, post, tags


# ---------- Hacker News ----------
AI_RE = re.compile(r"\b(ai|llm|llms|gpt|claude|gemini|agent|agents|openai|anthropic|model|open source)\b", re.I)


def hn_ai_items():
    try:
        ids = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=30).json()[:60]
        out = []
        for id_ in ids:
            it = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{id_}.json", timeout=30).json()
            t = (it or {}).get("title", "")
            if AI_RE.search(t):
                out.append(f"{t} - {it.get('url', '')}")
        return out[:5]
    except Exception as e:
        print("Hacker News fetch failed:", e)
        return []


# ---------- Groq ----------
GROQ_HEADERS = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
PREFERRED = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "llama-3.1-8b-instant", "openai/gpt-oss-20b"]


def groq_models():
    models = list(PREFERRED)
    try:
        mr = requests.get("https://api.groq.com/openai/v1/models", headers=GROQ_HEADERS, timeout=30)
        if mr.ok:
            available = [m["id"] for m in mr.json().get("data", [])]
            skip = ("whisper", "guard", "tts", "playai", "embed", "orpheus", "safeguard")
            chat = [m for m in available if not any(s in m.lower() for s in skip)]
            models = [m for m in PREFERRED if m in chat] + [m for m in chat if m not in PREFERRED]
        else:
            print("Could not list Groq models:", mr.status_code, mr.text)
    except Exception as e:
        print("Model list failed:", e)
    return models[:6]


def generate(task, context):
    fallback = None
    for model in groq_models():
        body = {
            "model": model,
            "temperature": 0.9,
            "max_completion_tokens": 2500,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": f"{task}\n\n{context}"}],
        }
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                              headers=GROQ_HEADERS, json=body, timeout=90)
        except Exception as e:
            print("Request error:", model, e)
            continue
        if not r.ok:
            print("Model failed:", model, r.status_code, r.text[:300])
            continue
        raw = (r.json()["choices"][0]["message"].get("content") or "").strip()
        res = parse(raw)
        if not res:
            print("Could not parse output, trying next:", model)
            continue
        headline, post, tags = res
        if not (150 <= len(post) <= 650) or not (2 <= len(headline.split()) <= 10):
            print("Bad length, trying next:", model)
            continue
        if len(tags) < 2:
            print("Too few hashtags, trying next:", model)
            continue
        if any(b in post.lower() for b in BANNED):
            print("Contains banned phrase, trying next:", model)
            fallback = fallback or (headline, post, tags, model)
            continue
        print("Used model:", model)
        return headline, post, tags
    if fallback:
        print("Using fallback from:", fallback[3])
        return fallback[:3]
    raise SystemExit("All Groq models failed, see errors above")


# ---------- image card ----------
W, H = 1200, 627
PALETTES = [
    ((15, 23, 42), (30, 64, 175), (56, 189, 248)),
    ((17, 24, 39), (88, 28, 135), (244, 114, 182)),
    ((2, 44, 34), (6, 95, 70), (52, 211, 153)),
    ((30, 27, 75), (49, 46, 129), (251, 191, 36)),
]
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def font(size):
    for p in FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def wrap(draw, text, f, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=f) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def make_card(headline, label, idx):
    top, bottom, accent = PALETTES[idx % len(PALETTES)]
    img = Image.new("RGB", (W, H), top)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        c = tuple(int(top[k] + (bottom[k] - top[k]) * t) for k in range(3))
        d.line([(0, y), (W, y)], fill=c)

    # logo on a white rounded square so it shows on any background
    d.rounded_rectangle((66, 52, 176, 162), radius=20, fill="white")
    if os.path.exists("logo.png"):
        logo = Image.open("logo.png").convert("RGBA").resize((92, 92))
        img.paste(logo, (75, 61), logo)
    d.text((196, 88), "Hexalogic Tech Solutions", font=font(32), fill="white")

    # label chip, top right
    lf = font(24)
    lw = d.textlength(label, font=lf)
    d.rounded_rectangle((W - 70 - lw - 40, 70, W - 70, 124), radius=27, outline=accent, width=3)
    d.text((W - 70 - lw - 20, 83), label, font=lf, fill=accent)

    # headline, auto-fit
    size = 92
    while size >= 48:
        f = font(size)
        lines = wrap(d, headline, f, 1060)
        if len(lines) <= 4 and len(lines) * size * 1.2 <= 320:
            break
        size -= 4
    d.rectangle((70, 215, 190, 224), fill=accent)
    y = 250
    for ln in lines:
        d.text((70, y), ln, font=f, fill="white")
        y += int(size * 1.2)

    d.text((70, H - 70), "hexalogictechandsolutions.com", font=font(26), fill=(203, 213, 225))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- LinkedIn ----------
def li_escape(s):
    # escape characters LinkedIn treats as formatting so posts are not cut off
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
        r = requests.request(method, url, headers=headers, timeout=60, **kw)
        if r.status_code == 426:
            print("LinkedIn version not active, trying older:", ver)
            continue
        VERSION = ver
        return r
    raise SystemExit("No active LinkedIn API version found, check LinkedIn's docs")


def upload_image(png):
    r = li("POST", "https://api.linkedin.com/rest/images?action=initializeUpload",
           json={"initializeUploadRequest": {"owner": AUTHOR}})
    if not r.ok:
        raise RuntimeError(f"initializeUpload {r.status_code} {r.text[:300]}")
    v = r.json()["value"]
    u = requests.put(v["uploadUrl"], data=png, timeout=120,
                     headers={"Authorization": f"Bearer {LI_TOKEN}",
                              "Content-Type": "application/octet-stream"})
    if not u.ok:
        raise RuntimeError(f"image upload {u.status_code} {u.text[:300]}")
    return v["image"]


def main():
    for k in ("GROQ_API_KEY", "LINKEDIN_TOKEN", "LINKEDIN_PERSON_ID"):
        if not os.environ.get(k, "").strip():
            raise SystemExit(f"Missing secret: {k}")

    state = json.load(open("state.json"))
    i = state["n"]
    recent = state.get("recent", [])
    kind = KINDS[i % len(KINDS)]
    topic = None

    if kind == "ai_tool":
        items = hn_ai_items()
        if items:
            context = "Recent items:\n" + "\n".join(items)
        else:
            kind = "new_tech"
    if kind != "ai_tool":
        pool = [t for t in TOPICS[kind] if t not in recent] or TOPICS[kind]
        topic = random.choice(pool)
        context = f"Topic: {topic}"

    headline, post, tags = generate(TASKS[kind], context)
    print("Headline:", headline)

    commentary = li_escape(post)
    if tags:
        commentary += "\n\n" + " ".join("{hashtag|\\#|%s}" % t for t in tags)

    payload = {
        "author": AUTHOR,
        "commentary": commentary,
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED",
                         "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }

    try:
        png = make_card(headline, LABELS[kind], i)
        urn = upload_image(png)
        payload["content"] = {"media": {"title": headline[:200], "altText": headline[:200], "id": urn}}
        print("Image uploaded:", urn)
    except Exception as e:
        print("Image step failed, posting text only:", e)

    p = li("POST", "https://api.linkedin.com/rest/posts", json=payload)
    if not p.ok:
        print("LinkedIn error:", p.status_code, p.text)
        raise SystemExit(1)
    print("Posted using LinkedIn version", VERSION)

    state["n"] = i + 1
    if topic:
        state["recent"] = (recent + [topic])[-10:]
    json.dump(state, open("state.json", "w"))
    print("Posted successfully:\n", post, "\n", tags)


if __name__ == "__main__":
    main()
