import os, sys, json, random, requests

sys.stdout.reconfigure(line_buffering=True)

GROQ_KEY = os.environ["GROQ_API_KEY"].strip()
LI_TOKEN = os.environ["LINKEDIN_TOKEN"].strip()
LI_PERSON = os.environ["LINKEDIN_PERSON_ID"].strip()

state = json.load(open("state.json"))
i = state["n"]

TYPES = ["fullstack_tip", "dev_story", "ai_tool", "lesson", "ai_tool", "fullstack_tip"]
kind = TYPES[i % len(TYPES)]

TOPICS = {
    "fullstack_tip": ["Next.js caching", "FastAPI vs Node.js", "REST vs tRPC", "Postgres indexing",
                      "Docker for dev environments", "auth best practices", "React Server Components"],
    "dev_story": ["a deployment that broke on Friday", "scope creep on a client project",
                  "a slow page that turned out to be one bad query", "choosing MVP features"],
    "lesson": ["estimating projects", "client communication", "code review habits", "technical debt"],
}


def hn_ai_items():
    try:
        ids = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=30).json()[:60]
        out = []
        for id_ in ids:
            it = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{id_}.json", timeout=30).json()
            t = (it or {}).get("title", "")
            if any(k in t.lower() for k in ["ai", "llm", "claude", "gpt", "agent", "model", "open source"]):
                out.append(f"{t} - {it.get('url', '')}")
        return out[:5]
    except Exception as e:
        print("Hacker News fetch failed:", e)
        return []


items = []
if kind == "ai_tool":
    items = hn_ai_items()
    if not items:
        kind = "fullstack_tip"

if kind == "ai_tool":
    context = "Recent items:\n" + "\n".join(items)
    task = "Pick the ONE most useful item for developers and write about it. Only mention facts present in the items."
else:
    topic = random.choice(TOPICS[kind])
    context = f"Topic: {topic}"
    task = {
        "fullstack_tip": "Write a practical tip with a tiny code idea or concrete example.",
        "dev_story": "Write a short, relatable lesson-style story. Keep it generic; do not invent specific client names, numbers or results.",
        "lesson": "Write a lesson learned from building software for clients.",
    }[kind]

SYSTEM = ("You write LinkedIn posts for Hexalogic Tech Solutions, a software house (web, apps, AI, automation). "
          "Tone: clear, human, no hype, no emoji spam (max 2), no hashtags except 3 at the end. "
          "Start with a strong one-line hook. 900-1300 characters. Short paragraphs. End with a question. "
          "Never invent statistics, client names or product features. Output only the post text, nothing else.")

# ---------- Groq: try models until one returns real text ----------
GROQ_HEADERS = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
PREFERRED = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]

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

text = None
for model in models[:6]:
    body = {
        "model": model,
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
    content = (r.json()["choices"][0]["message"].get("content") or "").strip()
    if len(content) < 200:
        print("Model returned empty or too-short text, trying next:", model)
        continue
    text = content
    print("Used model:", model)
    break

if not text:
    raise SystemExit("All Groq models failed, see errors above")

# ---------- LinkedIn: escape reserved characters so posts are not cut off ----------
def li_escape(s):
    for ch in ['\\', '|', '{', '}', '@', '[', ']', '(', ')', '<', '>', '*', '_', '~']:
        s = s.replace(ch, '\\' + ch)
    return s


p = requests.post(
    "https://api.linkedin.com/rest/posts",
    headers={"Authorization": f"Bearer {LI_TOKEN}", "LinkedIn-Version": "202504",
             "X-Restli-Protocol-Version": "2.0.0", "Content-Type": "application/json"},
    json={"author": f"urn:li:person:{LI_PERSON}", "commentary": li_escape(text),
          "visibility": "PUBLIC",
          "distribution": {"feedDistribution": "MAIN_FEED",
                           "targetEntities": [], "thirdPartyDistributionChannels": []},
          "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False},
    timeout=60,
)
if not p.ok:
    print("LinkedIn error:", p.status_code, p.text)
    raise SystemExit(1)

state["n"] = i + 1
json.dump(state, open("state.json", "w"))
print("Posted successfully:\n", text)
