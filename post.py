import os, json, random, requests

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
    ids = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=30).json()[:60]
    out = []
    for id_ in ids:
        it = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{id_}.json", timeout=30).json()
        t = (it or {}).get("title", "")
        if any(k in t.lower() for k in ["ai", "llm", "claude", "gpt", "agent", "model", "open source"]):
            out.append(f"{t} - {it.get('url','')}")
    return out[:5]

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
          "Tone: clear, human, no hype, no emojis spam (max 2), no hashtags except 3 at the end. "
          "Start with a strong one-line hook. 900-1300 characters. Short paragraphs. End with a question. "
          "Never invent statistics, client names or product features.")

r = requests.post(
    "https://api.groq.com/openai/v1/chat/completions",
    headers={"Authorization": f"Bearer {GROQ_KEY}"},
    json={"model": "llama-3.3-70b-versatile",  # if this errors, pick a current model from console.groq.com/docs/models
          "messages": [{"role": "system", "content": SYSTEM},
                       {"role": "user", "content": f"{task}\n\n{context}"}]},
    timeout=60,
)
r.raise_for_status()
text = r.json()["choices"][0]["message"]["content"].strip()

p = requests.post(
    "https://api.linkedin.com/rest/posts",
    headers={"Authorization": f"Bearer {LI_TOKEN}", "LinkedIn-Version": "202504",
             "X-Restli-Protocol-Version": "2.0.0", "Content-Type": "application/json"},
    json={"author": f"urn:li:person:{LI_PERSON}", "commentary": text, "visibility": "PUBLIC",
          "distribution": {"feedDistribution": "MAIN_FEED"}, "lifecycleState": "PUBLISHED"},
    timeout=60,
)
if not p.ok:
    print(p.status_code, p.text)
p.raise_for_status()

state["n"] = i + 1
json.dump(state, open("state.json", "w"))
print(text)
