# -*- coding: utf-8 -*-
"""
Kick Chat Panel — Railway Edition
INSTALL:  pip install -r requirements.txt
RUN:      python server.py
"""

import os, json, time, random, threading
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False
import tls_client, uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Any, Optional

try:
    import google.generativeai as genai
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

try:
    from groq import Groq
    HAS_GROQ = True
except ImportError:
    HAS_GROQ = False

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
TOKENS_FILE = os.path.join(BASE_DIR, "tokens.txt")

# Load tokens from env var if present (Railway deployment)
def _init_tokens_from_env():
    env_tokens = os.environ.get("TOKENS", "")
    if env_tokens and not os.path.exists(TOKENS_FILE):
        tokens = [t.strip() for t in env_tokens.split(",") if t.strip()]
        if tokens:
            with open(TOKENS_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(tokens) + "\n")
            print(f"[init] Loaded {len(tokens)} tokens from TOKENS env var")

_init_tokens_from_env()

app = FastAPI(title="Kick Chat Panel")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/", response_class=HTMLResponse)
async def root():
    with open(os.path.join(BASE_DIR, "static", "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

# ── MODELS ──────────────────────────────────────────────────────────────────────
class ConnectRequest(BaseModel):
    channel: str

class CheckTokenRequest(BaseModel):
    token: str
    proxy: Optional[str] = None

class SendRequest(BaseModel):
    chatroom_id: int
    message: str
    token: str
    channel: str = ""

class MultiSendRequest(BaseModel):
    chatroom_id: int
    words: List[str]
    tokens: List[str]
    channel: str = ""
    delay_ms: int = 500

class SaveTokensRequest(BaseModel):
    tokens: List[str]

class AIGenerateRequest(BaseModel):
    provider: str = "gemini"
    prompt: str
    api_key: Optional[str] = ''
    model: Optional[str] = None
    count: int = 5

class CheckFollowsRequest(BaseModel):
    target_id: Optional[Any] = None
    target_channel: Optional[str] = None
    tokens: List[str]

class MassFollowRequest(BaseModel):
    target_channel: str
    tokens: List[str]
    delay_ms: int = 2000

# ── TLS SESSION ───────────────────────────────────────────────────────────────
def parse_proxy_string(proxy: str):
    if not proxy: return None
    s = proxy.strip().rstrip(".")
    for prefix in ("http://", "https://"):
        if s.startswith(prefix): s = s[len(prefix):]
    d = {}
    if "@" in s:
        auth, server = s.rsplit("@", 1)
        if ":" in auth: d["username"], d["password"] = auth.split(":", 1)
        d["server"] = f"http://{server}"
    elif s.count(":") == 3:
        host, port, user, pw = s.split(":")
        d["server"] = f"http://{host}:{port}"; d["username"] = user; d["password"] = pw
    else:
        d["server"] = f"http://{s}"
    return d

def make_session(token: str = None, channel: str = "", proxy: str = None) -> tls_client.Session:
    session = tls_client.Session(client_identifier="chrome127", random_tls_extension_order=True)
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "referer": f"https://kick.com/{channel}" if channel else "https://kick.com/",
        "origin": "https://kick.com",
    }
    if token:
        bare = token.strip()
        if ":" in bare: bare = bare.split(":")[-1]
        if "|" in bare: bare = bare.split("|")[-1]
        headers["authorization"] = f"Bearer {bare}"
    session.headers.update(headers)
    if proxy:
        pd = parse_proxy_string(proxy)
        if pd:
            auth = f"{pd['username']}:{pd['password']}@" if pd.get("username") else ""
            url = pd["server"].replace("http://", f"http://{auth}")
            session.proxies = {"http": url, "https": url}
    return session

# ── USERNAME CACHE ────────────────────────────────────────────────────────────
CACHE_FILE = os.path.join(BASE_DIR, "username_cache.json")
username_cache = {}

def load_disk_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def lookup_username(token: str, proxy: str = None) -> dict:
    bare = token.strip()
    if ":" in bare: bare = bare.split(":")[-1]
    if "|" in bare: bare = bare.split("|")[-1]
    
    disk_cache = load_disk_cache()
    if bare in disk_cache:
        return disk_cache[bare]
    if bare in username_cache:
        return username_cache[bare]
    try:
        r = make_session(token=bare, proxy=proxy).get("https://kick.com/api/v1/user", timeout_seconds=8)
        if r.status_code == 200:
            data = r.json()
            username = data.get("username") or data.get("name") or ""
            uid = str(data.get("id", ""))
            if not username or username.lower() == "unknown":
                return {"error": "Unknown username — token likely invalid"}
            result = {"username": username, "id": uid}
            username_cache[bare] = result
            disk_cache[bare] = result
            try:
                with open(CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(disk_cache, f, indent=2)
            except Exception:
                pass
            return result
        return {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}

# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.post("/api/connect")
async def connect_channel(req: ConnectRequest):
    channel = req.channel.strip().lower().split("/")[-1]
    try:
        r = make_session(channel=channel).get(f"https://kick.com/api/v2/channels/{channel}", timeout_seconds=10)
        if r.status_code == 200:
            data = r.json()
            return {"ok": True, "chatroom_id": data["chatroom"]["id"], "user_id": data["id"], "channel": channel}
        r = make_session(channel=channel).get(f"https://kick.com/api/v2/channels/{channel}/chatroom", timeout_seconds=10)
        if r.status_code == 200:
            return {"ok": True, "chatroom_id": r.json()["id"], "user_id": None, "channel": channel}
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/check_token")
async def check_token(req: CheckTokenRequest):
    result = lookup_username(req.token, req.proxy)
    if "error" in result:
        return {"ok": False, "valid": False, "error": result["error"]}
    return {"ok": True, "valid": True, "username": result["username"], "id": result["id"]}


def get_channel_user_id(slug: str) -> str:
    slug = slug.lower().split("/")[-1]
    try:
        r = make_session(channel=slug).get(f"https://kick.com/api/v2/channels/{slug}", timeout_seconds=10)
        if r.status_code == 200:
            return str(r.json().get("id", ""))
        return ""
    except:
        return ""


@app.post("/api/follow_mass")
async def follow_mass(req: MassFollowRequest):
    if not req.tokens: return {"ok": False, "error": "No tokens provided"}
    channel_slug = req.target_channel.strip().lower().split("/")[-1]
    if not channel_slug:
        return {"ok": False, "error": f"Invalid channel '{req.target_channel}'"}

    results = []

    def do_follow(token, idx):
        bare = token.strip()
        if ":" in bare: bare = bare.split(":")[-1]
        if "|" in bare: bare = bare.split("|")[-1]
        session = make_session(token=bare, channel=channel_slug)
        try:
            session.get(f"https://kick.com/{channel_slug}", timeout_seconds=10)
            xsrf_cookie = session.cookies.get("XSRF-TOKEN")
            if xsrf_cookie:
                import urllib.parse
                session.headers["X-Xsrf-Token"] = urllib.parse.unquote(xsrf_cookie)
            r = session.post(f"https://kick.com/api/v2/channels/{channel_slug}/follow", json={}, timeout_seconds=10)
            ok = r.status_code in (200, 201, 204)
            already = False
            if not ok and r.status_code == 422:
                if "already following" in r.text.lower():
                    ok = True
                    already = True
            status = "already followed" if already else ("followed" if ok else f"HTTP {r.status_code}")
            if not ok and "Just a moment" in r.text: status = "Cloudflare Block"
            elif r.status_code == 429: status = "Rate Limited"
            elif r.status_code == 401: status = "Invalid Token"
            results.append({"index": idx, "ok": ok, "status": status})
        except Exception as e:
            results.append({"index": idx, "ok": False, "status": str(e)})

    for i, token in enumerate(req.tokens):
        if i > 0 and req.delay_ms > 0: time.sleep(req.delay_ms / 1000)
        t = threading.Thread(target=do_follow, args=(token, i + 1), daemon=True)
        t.start(); t.join()

    return {"ok": True, "followed": sum(1 for r in results if r["ok"]), "results": results}


# Stealth follow — uses Playwright browser to bypass Cloudflare/Kasada
@app.post("/api/follow_stealth")
async def follow_stealth(req: MassFollowRequest):
    print(f"[FOLLOW] called — PLAYWRIGHT_OK={PLAYWRIGHT_OK}, tokens={len(req.tokens) if req.tokens else 0}, channel={req.target_channel}")
    if not PLAYWRIGHT_OK:
        print("[FOLLOW] Playwright not available")
        return {"ok": False, "error": "Playwright not installed — add playwright to requirements.txt and redeploy."}
    if not req.tokens:
        print("[FOLLOW] No tokens provided")
        return {"ok": False, "error": "No tokens provided"}

    channel_slug = req.target_channel.strip().lower().split("/")[-1]
    if not channel_slug:
        return {"ok": False, "error": "Invalid channel name"}

    print(f"[FOLLOW] Starting {len(req.tokens)} browser follows")
    results = []

    def do_browser_follow(token, idx):
        print(f"[FOLLOW] Starting browser follow for token {idx}")
        bare = token.strip()
        if ":" in bare: bare = bare.split(":")[-1]
        if "|" in bare: bare = bare.split("|")[-1]
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=[
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ])
                ctx = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 720},
                    locale="en-US",
                )
                page = ctx.new_page()

                # Navigate to channel page to get fresh XSRF + cookies
                page.goto(f"https://kick.com/{channel_slug}", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

                import urllib.parse
                cookies = ctx.cookies()
                xsrf = next((c["value"] for c in cookies if c["name"] == "XSRF-TOKEN"), None)
                if xsrf:
                    xsrf = urllib.parse.unquote(xsrf)
                print(f"[FOLLOW] Token {idx} XSRF: {xsrf[:20] if xsrf else 'NONE'}")

                xsrf_header = f"'X-Xsrf-Token': '{xsrf}'," if xsrf else ""
                js_result = page.evaluate(f"""async () => {{
                    const r = await fetch('https://kick.com/api/v2/channels/{channel_slug}/follow', {{
                        method: 'POST',
                        headers: {{
                            'Authorization': 'Bearer {bare}',
                            'Content-Type': 'application/json',
                            'Accept': 'application/json',
                            'Origin': 'https://kick.com',
                            'Referer': 'https://kick.com/{channel_slug}',
                            {xsrf_header}
                        }},
                        body: JSON.stringify({{}})
                    }});
                    let body = '';
                    try {{ body = await r.text(); }} catch(e) {{}}
                    return {{ status: r.status, ok: r.ok, body: body.slice(0, 300) }};
                }}""")
                print(f"[FOLLOW] Token {idx} response body: {js_result.get('body', '')}")

                ok = js_result.get("ok", False)
                body = js_result.get("body", "")
                already = False
                if not ok and ("already following" in body.lower() or "already followed" in body.lower()):
                    ok = True
                    already = True
                status = "already followed" if already else ("followed" if ok else f"HTTP {js_result.get('status', '?')}")
                print(f"[FOLLOW] Token {idx}: {status} — raw: {js_result}")
                results.append({"index": idx, "ok": ok, "status": status})
                browser.close()
        except Exception as e:
            print(f"[FOLLOW] Token {idx} ERROR: {e}")
            results.append({"index": idx, "ok": False, "status": str(e)})

    for i, token in enumerate(req.tokens):
        if i > 0 and req.delay_ms > 0:
            time.sleep(req.delay_ms / 1000)
        t = threading.Thread(target=do_browser_follow, args=(token, i + 1), daemon=True)
        t.start(); t.join()

    return {"ok": True, "followed": sum(1 for r in results if r["ok"]), "results": results}


@app.post("/api/check_follows")
async def check_follows(req: CheckFollowsRequest):
    if not req.tokens: return {"ok": False, "error": "No tokens provided"}
    channel_slug = (req.target_channel or "").strip().lower().split("/")[-1]
    if not channel_slug and req.target_id:
        try:
            r_ch = make_session().get(f"https://kick.com/api/v2/channels/{req.target_id}", timeout_seconds=8)
            if r_ch.status_code == 200:
                channel_slug = r_ch.json().get("slug", "")
        except Exception:
            channel_slug = str(req.target_id)
    if not channel_slug:
        return {"ok": False, "error": "target_channel or target_id missing"}

    results = []
    for i, token in enumerate(req.tokens):
        bare = token.strip()
        if ":" in bare: bare = bare.split(":")[-1]
        if "|" in bare: bare = bare.split("|")[-1]
        session = make_session(token=bare, channel=channel_slug)
        try:
            r = session.get(f"https://kick.com/api/v2/channels/{channel_slug}/me", timeout_seconds=10)
            if r.status_code == 200:
                following = r.json().get("is_following", False)
                results.append({"token": token, "following": following})
            else:
                results.append({"token": token, "following": False, "status": f"HTTP {r.status_code}"})
        except Exception as e:
            results.append({"token": token, "following": False, "error": str(e)})
        if (i + 1) % 5 == 0: time.sleep(0.1)
    return {"ok": True, "results": results}


# ── KICK EMOTE SYSTEM & FORMATTER ──────────────────────────────────────────
EMOTES_CACHE_FILE = os.path.join(BASE_DIR, "emotes_cache.json")
KICK_EMOTE_REGISTRY = {}      # lowercase_name -> (id, exact_name)
CHANNEL_EMOTES_CACHE = {}     # channel_slug -> {lowercase_name -> (id, exact_name)}

KICK_ALIASES = {
    "kickpog": (37233, "PogU"),
    "pog": (37233, "PogU"),
    "poggers": (37233, "PogU"),
    "kickyes": (5756504, "NODDERS"),
    "nodders": (5756504, "NODDERS"),
    "kicksad": (4148081, "Sadge"),
    "sadge": (4148081, "Sadge"),
    "kickcry": (2248821, "globalcry"),
    "globalcry": (2248821, "globalcry"),
    "kickchamp": (5756678, "WeirdChamp"),
    "champ": (5756678, "WeirdChamp"),
    "weirdchamp": (5756678, "WeirdChamp"),
    "kickbruh": (4147910, "BBoomer"),
    "bruh": (4147910, "BBoomer"),
    "bboomer": (4147910, "BBoomer"),
    "kicklove": (1730794, "emojiBlowKiss"),
    "emojiblowkiss": (1730794, "emojiBlowKiss"),
    "kickrage": (1730809, "emojiEnraged"),
    "emojienraged": (1730809, "emojiEnraged"),
    "kickno": (1730823, "emojiNo"),
    "emojino": (1730823, "emojiNo"),
    "kicklaugh": (2248827, "globallol"),
    "globallol": (2248827, "globallol"),
    "kickwtf": (2248835, "globalwhat"),
    "globalwhat": (2248835, "globalwhat"),
    "kickthink": (1730814, "emojiHmm"),
    "emojihmm": (1730814, "emojiHmm"),
    "kekw": (37226, "KEKW"),
    "lulw": (37227, "LULW"),
    "prayge": (37234, "Prayge"),
    "bang": (5882722, "BANG"),
    "ayaya": (5756662, "AYAYA"),
    "ez": (5756668, "EZ"),
    "gigachad": (5882725, "GIGACHAD"),
    "clap": (5756667, "Clap"),
    "hyperclap": (5756670, "HYPERCLAP"),
    "catjam": (5756644, "ratJAM"),
    "ratjam": (5756644, "ratJAM"),
    "emojifire": (1730812, "emojiFire"),
    "emojiyes": (1730838, "emojiYes"),
    "w": (5882722, "BANG"),
    "fire": (1730812, "emojiFire"),
    "sheesh": (5882725, "GIGACHAD")
}

def init_kick_emotes():
    if os.path.exists(EMOTES_CACHE_FILE):
        try:
            with open(EMOTES_CACHE_FILE, "r", encoding="utf-8") as f:
                groups = json.load(f)
            for grp in groups:
                for e in grp.get("emotes", []):
                    eid = e.get("id")
                    ename = e.get("name")
                    if eid and ename:
                        KICK_EMOTE_REGISTRY[ename.lower()] = (eid, ename)
            print(f"[init] Loaded {len(KICK_EMOTE_REGISTRY)} official Kick emotes into registry")
        except Exception as ex:
            print(f"[init] Failed to load emotes cache: {ex}")

init_kick_emotes()

def format_kick_message(message: str, channel: str = "") -> str:
    if not message:
        return message
    raw = str(message).strip()
    if raw.startswith("[emote:") and raw.endswith("]"):
        return raw

    ch_slug = (channel or "").strip().lower().split("/")[-1]
    ch_reg = CHANNEL_EMOTES_CACHE.get(ch_slug, {})

    def lookup_emote(token: str):
        tl = token.lower()
        if ch_reg and tl in ch_reg:
            return ch_reg[tl]
        if tl in KICK_ALIASES:
            return KICK_ALIASES[tl]
        if tl in KICK_EMOTE_REGISTRY:
            return KICK_EMOTE_REGISTRY[tl]
        return None

    # Check if the entire message is an emote or alias
    single = lookup_emote(raw)
    if single:
        return f"[emote:{single[0]}:{single[1]}]"

    # Multi-token sentence check
    words = raw.split()
    changed = False
    new_words = []
    skip_tokens = {"sit", "me", "he", "she", "it", "so", "up", "on", "in", "to", "at", "by", "for"}
    for w in words:
        wl = w.lower()
        if wl not in skip_tokens:
            found = lookup_emote(w)
            if found:
                new_words.append(f"[emote:{found[0]}:{found[1]}]")
                changed = True
                continue
        new_words.append(w)

    return " ".join(new_words) if changed else raw


@app.post("/api/send")
async def send_message(req: SendRequest):
    content = format_kick_message(req.message, req.channel)
    print(f"[SEND] chatroom={req.chatroom_id} channel={req.channel} msg={req.message[:50]} -> kick_content={content[:60]}")
    bare = req.token.strip()
    if ":" in bare: bare = bare.split(":")[-1]
    if "|" in bare: bare = bare.split("|")[-1]
    session = make_session(token=bare, channel=req.channel)
    session.headers["content-type"] = "application/json"
    try:
        r = session.post(f"https://kick.com/api/v2/messages/send/{req.chatroom_id}",
                         json={"content": content, "type": "message"}, timeout_seconds=10)
        print(f"[SEND] response status={r.status_code}")
        if r.status_code in (200, 201): 
            print(f"[SEND] ✓ Message sent")
            return {"ok": True}
        if r.status_code == 429: return {"ok": False, "error": "Rate limited"}
        if r.status_code == 401: return {"ok": False, "error": "Token invalid"}
        if r.status_code == 400: return {"ok": False, "error": f"Bad request (slow mode/sub-only/channel rule)"}
        if r.status_code == 403:
            return {"ok": False, "error": "Cloudflare block" if "Just a moment" in r.text else f"Forbidden: {r.text[:80]}"}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:100]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/multi_send")
async def multi_send(req: MultiSendRequest):
    if not req.words: return {"ok": False, "error": "No words provided"}
    if not req.tokens: return {"ok": False, "error": "No tokens provided"}
    results = []
    def send_one(token, message, idx):
        bare = token.strip()
        if ":" in bare: bare = bare.split(":")[-1]
        if "|" in bare: bare = bare.split("|")[-1]
        session = make_session(token=bare, channel=req.channel)
        session.headers["content-type"] = "application/json"
        content = format_kick_message(message, req.channel)
        try:
            r = session.post(f"https://kick.com/api/v2/messages/send/{req.chatroom_id}",
                             json={"content": content, "type": "message"}, timeout_seconds=10)
            ok = r.status_code in (200, 201)
            results.append({"index": idx, "ok": ok, "message": message, "status": "ok" if ok else f"HTTP {r.status_code}"})
        except Exception as e:
            results.append({"index": idx, "ok": False, "message": message, "status": str(e)})
    for i, token in enumerate(req.tokens):
        if i > 0 and req.delay_ms > 0: time.sleep(req.delay_ms / 1000)
        t = threading.Thread(target=send_one, args=(token, random.choice(req.words), i + 1), daemon=True)
        t.start(); t.join()
    sent = sum(1 for r in results if r["ok"])
    return {"ok": True, "sent": sent, "failed": len(results) - sent, "results": results}


@app.post("/api/ai_generate")
async def ai_generate(req: AIGenerateRequest):
    system_prompt = (
        f"You are a human chatter in a Kick.com stream. Generate exactly {req.count} unique, human-like messages "
        f"based on this topic: '{req.prompt}'.\n"
        "RULES:\n- Extremely short: 1-4 words only.\n- Casual: lowercase, slang, typos.\n"
        "- Varied: no repeated words.\n- Output ONLY a raw JSON array of strings, nothing else."
    )
    try:
        provider = req.provider.lower().strip()
        if provider != "lmstudio" and not req.api_key:
            return {"ok": False, "error": f"API Key required for {provider}"}

        if provider == "gemini":
            if not HAS_GENAI: return {"ok": False, "error": "pip install google-generativeai"}
            genai.configure(api_key=req.api_key)
            text = genai.GenerativeModel('gemini-1.5-flash').generate_content(system_prompt).text.strip()
        elif provider == "groq":
            if not HAS_GROQ: return {"ok": False, "error": "pip install groq"}
            text = Groq(api_key=req.api_key).chat.completions.create(
                messages=[{"role": "user", "content": system_prompt}], model="llama-3.3-70b-versatile"
            ).choices[0].message.content.strip()
        elif provider in ("chatgpt", "deepseek"):
            url = "https://api.openai.com/v1/chat/completions" if provider == "chatgpt" else "https://api.deepseek.com/v1/chat/completions"
            model_name = "gpt-3.5-turbo" if provider == "chatgpt" else "deepseek"
            s = tls_client.Session(client_identifier="chrome127", random_tls_extension_order=True)
            s.headers.update({"Authorization": f"Bearer {req.api_key}", "Content-Type": "application/json"})
            r = s.post(url, json={"model": model_name, "messages": [{"role": "system", "content": system_prompt}], "max_tokens": 300, "temperature": 0.8}, timeout_seconds=20)
            if r.status_code != 200: return {"ok": False, "error": f"{provider} error {r.status_code}: {r.text[:200]}"}
            text = r.json()["choices"][0]["message"]["content"].strip()
        elif provider == "lmstudio":
            url = (req.api_key or "http://127.0.0.1:11434").strip()
            if not url.startswith("http"): url = "http://" + url
            s = tls_client.Session(client_identifier="chrome127", random_tls_extension_order=True)
            s.headers.update({"Content-Type": "application/json"})
            r = s.post(f"{url}/v1/chat/completions", json={"model": req.model or "local", "messages": [{"role": "user", "content": system_prompt}], "max_tokens": 300, "temperature": 0.8}, timeout_seconds=120)
            if r.status_code != 200: return {"ok": False, "error": f"LM Studio error {r.status_code}: {r.text[:200]}"}
            text = r.json()["choices"][0]["message"]["content"].strip()
        else:
            return {"ok": False, "error": f"Unknown provider: {provider}"}

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"): text = text[4:]
            text = text.strip("` \n")
        messages = json.loads(text)
        if not isinstance(messages, list): return {"ok": False, "error": "AI returned invalid format"}
        return {"ok": True, "messages": messages[:req.count]}
    except Exception as e:
        return {"ok": False, "error": f"AI Error: {str(e)}"}


# ── TOKEN FILE ROUTES ─────────────────────────────────────────────────────────

@app.get("/api/tokens/load")
async def load_tokens():
    if not os.path.exists(TOKENS_FILE):
        return {"ok": True, "tokens": [], "accounts": []}
    with open(TOKENS_FILE, "r", encoding="utf-8") as f:
        tokens = [line.strip() for line in f if line.strip()]
    
    disk_cache = load_disk_cache()
    accounts_list = []
    for i, t in enumerate(tokens):
        bare = t.strip()
        if ":" in bare: bare = bare.split(":")[-1]
        if "|" in bare: bare = bare.split("|")[-1]
        info = disk_cache.get(bare, {})
        username = info.get("username") if isinstance(info, dict) else ""
        uid = info.get("id") if isinstance(info, dict) else ""
        if not username:
            username = f"acc_{i+1}"
        accounts_list.append({
            "index": i + 1,
            "token": t,
            "valid": True,
            "username": username,
            "id": str(uid)
        })
    return {"ok": True, "tokens": tokens, "accounts": accounts_list}

@app.post("/api/tokens/save")
async def save_tokens(req: SaveTokensRequest):
    clean = [t.strip() for t in req.tokens if t.strip()]
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(clean) + ("\n" if clean else ""))
    return {"ok": True, "saved": len(clean)}

@app.get("/api/proxies/status")
async def proxy_status():
    return {"ok": True, "count": 0}

# Bot/browser endpoints — not available on Railway
@app.get("/api/bot/status")
async def bot_status():
    return {"ok": True, "running": False, "pid": None}

@app.post("/api/bot/start")
async def bot_start():
    return {"ok": False, "error": "Bot not available on Railway deployment"}

@app.post("/api/bot/stop")
async def bot_stop():
    return {"ok": False, "error": "Bot not available on Railway deployment"}

@app.post("/api/open_browser")
async def open_browser(req: dict = {}):
    return {"ok": False, "error": "Browser not available on Railway deployment"}

@app.get("/api/emotes")
async def get_emotes(channel: str = ""):
    slug = (channel or "").strip().lower().split("/")[-1]
    if slug and slug != "global":
        try:
            s = make_session(channel=slug)
            r = s.get(f"https://kick.com/emotes/{slug}", timeout_seconds=6)
            if r.status_code == 200:
                data = r.json()
                groups = []
                for grp in data:
                    grp_name = grp.get("name") or grp.get("slug") or "Emotes"
                    emotes = []
                    for e in grp.get("emotes", []):
                        eid = e.get("id")
                        ename = e.get("name")
                        if eid and ename:
                            emotes.append({
                                "id": eid,
                                "name": ename,
                                "url": f"https://files.kick.com/emotes/{eid}/fullsize",
                                "subscribers_only": e.get("subscribers_only", False)
                            })
                            if slug not in CHANNEL_EMOTES_CACHE:
                                CHANNEL_EMOTES_CACHE[slug] = {}
                            CHANNEL_EMOTES_CACHE[slug][ename.lower()] = (eid, ename)
                    if emotes:
                        groups.append({"group": grp_name, "emotes": emotes})
                if groups:
                    return {"ok": True, "groups": groups}
        except Exception:
            pass

    if os.path.exists(EMOTES_CACHE_FILE):
        try:
            with open(EMOTES_CACHE_FILE, "r", encoding="utf-8") as f:
                cached_groups = json.load(f)
            return {"ok": True, "groups": cached_groups}
        except Exception:
            pass

    return {"ok": True, "groups": []}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"\n  Kick Chat Panel — Railway Edition")
    print(f"  Running on port {port}\n")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
