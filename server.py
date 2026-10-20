# -*- coding: utf-8 -*-
"""
Kick Chat Panel — Railway Edition
INSTALL:  pip install -r requirements.txt
RUN:      python server.py
"""

import os, json, time, random, threading, re, requests
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
    force: bool = True

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
    proxies: Optional[List[str]] = []

class MassFollowRequest(BaseModel):
    target_channel: str
    tokens: List[str]
    delay_ms: int = 2000
    proxies: Optional[List[str]] = []

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
    session = tls_client.Session(client_identifier="chrome_120", random_tls_extension_order=True)
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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

def lookup_username(token: str, proxy: str = None, force: bool = False) -> dict:
    bare = token.strip()
    if ":" in bare: bare = bare.split(":")[-1]
    if "|" in bare: bare = bare.split("|")[-1]
    
    disk_cache = load_disk_cache()
    if not force:
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
                return {"error": "Unknown username - token likely invalid"}
            result = {"username": username, "id": uid}
            username_cache[bare] = result
            disk_cache[bare] = result
            try:
                with open(CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(disk_cache, f, indent=2)
            except Exception:
                pass
            return result
        
        # Invalidate dead or revoked tokens from disk cache
        if r.status_code in (401, 403, 404):
            if bare in disk_cache:
                del disk_cache[bare]
                try:
                    with open(CACHE_FILE, "w", encoding="utf-8") as f:
                        json.dump(disk_cache, f, indent=2)
                except Exception:
                    pass
            if bare in username_cache:
                del username_cache[bare]

        if r.status_code == 401:
            return {"error": "HTTP 401 (Token Invalid / Expired)"}
        elif r.status_code == 403:
            return {"error": "HTTP 403 (Cloudflare Block - Use Proxy)"}
        elif r.status_code == 429:
            return {"error": "HTTP 429 (Rate Limited - Slow Down)"}
        return {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}

# ── ROUTES ──

@app.post("/api/connect")
async def connect_channel(req: ConnectRequest):
    channel = req.channel.strip().lower().split("/")[-1]
    try:
        r = make_session(channel=channel).get(f"https://kick.com/api/v2/channels/{channel}", timeout_seconds=10)
        if r.status_code == 200:
            data = r.json()
            return {"ok": True, "chatroom_id": data["chatroom"]["id"], "user_id": data["id"], "channel_id": data["id"], "channel": channel}
        r = make_session(channel=channel).get(f"https://kick.com/api/v2/channels/{channel}/chatroom", timeout_seconds=10)
        if r.status_code == 200:
            return {"ok": True, "chatroom_id": r.json()["id"], "user_id": None, "channel": channel}
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}



@app.get("/api/chat/messages/{channel_id}")
async def get_channel_messages(channel_id: int):
    try:
        session = make_session()
        r = session.get(f"https://kick.com/api/v2/channels/{channel_id}/messages", timeout_seconds=8)
        if r.status_code == 200:
            data = r.json()
            messages = data.get("data", {}).get("messages", [])
            return {"ok": True, "messages": messages}
        return {"ok": False, "error": f"HTTP {r.status_code}", "messages": []}
    except Exception as e:
        return {"ok": False, "error": str(e), "messages": []}

@app.post("/api/check_token")
async def check_token(req: CheckTokenRequest):
    result = lookup_username(req.token, req.proxy, force=req.force)
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
    proxies = [p.strip() for p in (req.proxies or []) if p.strip()]

    def do_follow(token, idx, proxy_str=None):
        bare = token.strip()
        if ":" in bare: bare = bare.split(":")[-1]
        if "|" in bare: bare = bare.split("|")[-1]
        session = make_session(token=bare, channel=channel_slug, proxy=proxy_str)
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
            results.append({"index": idx, "ok": ok, "status": status, "proxy_used": bool(proxy_str)})
        except Exception as e:
            results.append({"index": idx, "ok": False, "status": str(e), "proxy_used": bool(proxy_str)})

    for i, token in enumerate(req.tokens):
        if i > 0 and req.delay_ms > 0:
            jitter = random.uniform(0.85, 1.45)
            time.sleep((req.delay_ms / 1000) * jitter)
        proxy_str = proxies[i % len(proxies)] if proxies else None
        t = threading.Thread(target=do_follow, args=(token, i + 1, proxy_str), daemon=True)
        t.start(); t.join()

    return {"ok": True, "followed": sum(1 for r in results if r["ok"]), "results": results}


# Stealth follow - uses Playwright browser to bypass Cloudflare/Kasada
@app.post("/api/follow_stealth")
async def follow_stealth(req: MassFollowRequest):
    print(f"[FOLLOW] called - PLAYWRIGHT_OK={PLAYWRIGHT_OK}, tokens={len(req.tokens) if req.tokens else 0}, channel={req.target_channel}")
    if not PLAYWRIGHT_OK:
        print("[FOLLOW] Playwright not available")
        return {"ok": False, "error": "Playwright not installed - add playwright to requirements.txt and redeploy."}
    if not req.tokens:
        print("[FOLLOW] No tokens provided")
        return {"ok": False, "error": "No tokens provided"}

    channel_slug = req.target_channel.strip().lower().split("/")[-1]
    if not channel_slug:
        return {"ok": False, "error": "Invalid channel name"}

    print(f"[FOLLOW] Starting {len(req.tokens)} browser follows")
    results = []
    proxies = [p.strip() for p in (req.proxies or []) if p.strip()]

    def do_browser_follow(token, idx, proxy_str=None):
        print(f"[FOLLOW] Starting browser follow for token {idx}")
        bare = token.strip()
        if ":" in bare: bare = bare.split(":")[-1]
        if "|" in bare: bare = bare.split("|")[-1]
        try:
            with sync_playwright() as p:
                launch_args = [
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ]
                launch_kwargs = {"headless": True, "args": launch_args}
                if proxy_str:
                    pd = parse_proxy_string(proxy_str)
                    if pd:
                        pw_proxy = {"server": pd["server"]}
                        if pd.get("username"):
                            pw_proxy["username"] = pd["username"]
                            pw_proxy["password"] = pd.get("password", "")
                        launch_kwargs["proxy"] = pw_proxy

                browser = p.chromium.launch(**launch_kwargs)
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
                print(f"[FOLLOW] Token {idx}: {status} - raw: {js_result}")
                results.append({"index": idx, "ok": ok, "status": status, "proxy_used": bool(proxy_str)})
                browser.close()
        except Exception as e:
            print(f"[FOLLOW] Token {idx} ERROR: {e}")
            results.append({"index": idx, "ok": False, "status": str(e), "proxy_used": bool(proxy_str)})

    for i, token in enumerate(req.tokens):
        if i > 0 and req.delay_ms > 0:
            jitter = random.uniform(0.85, 1.45)
            time.sleep((req.delay_ms / 1000) * jitter)
        proxy_str = proxies[i % len(proxies)] if proxies else None
        t = threading.Thread(target=do_browser_follow, args=(token, i + 1, proxy_str), daemon=True)
        t.start(); t.join()

    return {"ok": True, "followed": sum(1 for r in results if r["ok"]), "results": results}


@app.post("/api/check_follows")
async def check_follows(req: CheckFollowsRequest):
    if not req.tokens: return {"ok": False, "error": "No tokens provided"}
    channel_slug = (req.target_channel or "").strip().lower().split("/")[-1]
    proxies = [p.strip() for p in (req.proxies or []) if p.strip()]
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
        proxy_str = proxies[i % len(proxies)] if proxies else None
        session = make_session(token=bare, channel=channel_slug, proxy=proxy_str)
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
            s = tls_client.Session(client_identifier="chrome_120", random_tls_extension_order=True)
            s.headers.update({"Authorization": f"Bearer {req.api_key}", "Content-Type": "application/json"})
            r = s.post(url, json={"model": model_name, "messages": [{"role": "system", "content": system_prompt}], "max_tokens": 300, "temperature": 0.8}, timeout_seconds=20)
            if r.status_code != 200: return {"ok": False, "error": f"{provider} error {r.status_code}: {r.text[:200]}"}
            text = r.json()["choices"][0]["message"]["content"].strip()
        elif provider == "lmstudio":
            url = (req.api_key or "http://127.0.0.1:11434").strip()
            if not url.startswith("http"): url = "http://" + url
            s = tls_client.Session(client_identifier="chrome_120", random_tls_extension_order=True)
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



# ── MULTI-STREAM GLOBAL GIVEAWAY RADAR (SLOTS & CASINO DIRECTORY) ────────────
RADAR_WATCHLIST_FILE = "radar_watchlist.json"
DEFAULT_WATCHLIST = [
    "roshtein", "trainwreckstv", "classybeef", "deuceace", "xposed",
    "ayezee", "corinnakopf", "adinross", "stevewilldoit", "casinodaddy",
    "mellstroy", "bidule", "teuf", "brutalsimpson", "ac7ionman",
    "daskelelele", "yassuo", "m0e_tv", "cheesur", "fadesslots",
    "fruityslots", "hideousslots", "spintwix", "vitalythegoat", "westcol"
]

radar_state = {
    "enabled": True,
    "auto_enter": False,
    "auto_discover_slots": True,  # Automatically scans ALL live Slots & Casino streams on Kick
    "watchlist": list(DEFAULT_WATCHLIST),
    "live_slots_streamers": {},  # slug -> {channel_id, slug, username, viewers, title, profile_pic}
    "channel_cache": {},
    "processed_msg_ids": set(),
    "events": [],
    "last_discovery_time": 0
}

def load_radar_state():
    if os.path.exists(RADAR_WATCHLIST_FILE):
        try:
            with open(RADAR_WATCHLIST_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                radar_state["watchlist"] = d.get("watchlist", DEFAULT_WATCHLIST)
                radar_state["auto_enter"] = d.get("auto_enter", False)
                radar_state["enabled"] = d.get("enabled", True)
                radar_state["auto_discover_slots"] = d.get("auto_discover_slots", True)
        except Exception:
            pass

def save_radar_state():
    try:
        with open(RADAR_WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "watchlist": radar_state["watchlist"],
                "auto_enter": radar_state["auto_enter"],
                "enabled": radar_state["enabled"],
                "auto_discover_slots": radar_state.get("auto_discover_slots", True)
            }, f, indent=2)
    except Exception:
        pass

load_radar_state()

GIVEAWAY_PATTERNS = [
    re.compile(r"""(?:palabra\s+clave|keyword|kw|word|code|type|command|escribe|escribir)\s*(?:is|es|[:=\-])?\s*["']?([!#$]?[a-zA-Z0-9_\-]+)""", re.I),
    re.compile(r"""([!#$][a-zA-Z0-9_\-]+)\s*(?:to\s+enter|para\s+participar|to\s+win|for\s+giveaway|for\s+raffle)""", re.I),
    re.compile(r"""(?:giveaway|sorteo|raffle|drop).*(?:type|escribe|send|enter\s+with)\s*["']?([!#$]?[a-zA-Z0-9_\-]+)""", re.I),
    re.compile(r"""(?:giveaway|raffle|sorteo)\s+(?:keyword|word|kw)\s*(?:is|es|[:=\-])?\s*["']?([!#$]?[a-zA-Z0-9_\-]+)""", re.I),
    re.compile(r"""(?:type|command|kw)\s*:\s*["']?([!#$]?[a-zA-Z0-9_\-]+)""", re.I)
]

def fetch_live_slots_streamers(max_streams=250):
    """Fetches all live streams in the Slots & Casino category directly from Kick."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://kick.com',
        'x-app-platform': 'web'
    }
    discovered = []
    cursor = None
    page = 1
    while len(discovered) < max_streams:
        params = {
            'category_id': '28',
            'sort': 'viewer_count_desc',
            'limit': 50
        }
        if cursor:
            params['after'] = cursor
        try:
            r = requests.get('https://web.kick.com/api/v1/livestreams', params=params, headers=headers, timeout=8)
            if r.status_code != 200:
                break
            data = r.json()
            streams = data.get('data', {}).get('livestreams', [])
            if not streams:
                break
            for s in streams:
                ch = s.get('channel', {})
                ch_id = ch.get('id')
                slug = (ch.get('slug') or '').lower()
                if ch_id and slug:
                    discovered.append({
                        'channel_id': ch_id,
                        'slug': slug,
                        'username': ch.get('username') or slug,
                        'viewers': s.get('viewer_count') or 0,
                        'title': s.get('title') or '',
                        'profile_pic': ch.get('profile_pic') or ''
                    })
            next_cursor = data.get('data', {}).get('pagination', {}).get('next_cursor')
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            page += 1
        except Exception as e:
            break
    return discovered

def slots_discovery_worker():
    """Background thread that periodically refreshes the full list of live Slots & Casino streams."""
    while True:
        try:
            if radar_state.get("enabled") and radar_state.get("auto_discover_slots", True):
                streams = fetch_live_slots_streamers(max_streams=250)
                now = time.time()
                st_map = {}
                for s in streams:
                    slug = s["slug"]
                    st_map[slug] = s
                    existing_cache = radar_state["channel_cache"].get(slug, {})
                    radar_state["channel_cache"][slug] = {
                        "id": s["channel_id"],
                        "chatroom_id": existing_cache.get("chatroom_id"),
                        "is_live": True,
                        "category": "Slots & Casino",
                        "viewers": s["viewers"],
                        "title": s["title"],
                        "last_checked": now
                    }
                radar_state["live_slots_streamers"] = st_map
                radar_state["last_discovery_time"] = int(now)
                print(f"[RADAR] 🎰 Auto-discovered {len(st_map)} live Slots & Casino streamers on Kick.")
            time.sleep(45)
        except Exception as e:
            time.sleep(30)

threading.Thread(target=slots_discovery_worker, daemon=True).start()

def scan_channel_for_giveaway(target, session):
    try:
        now = time.time()
        if isinstance(target, dict):
            ch_slug = target.get("slug", "").lower()
            ch_id = target.get("channel_id")
        else:
            ch_slug = str(target).lower().replace("#", "")
            ch_id = None

        if not ch_slug:
            return

        cache = radar_state["channel_cache"].get(ch_slug, {})
        if not ch_id:
            ch_id = cache.get("id")

        # If channel ID unknown or cache expired for custom watchlist item, resolve from Kick
        if not ch_id or not cache or (now - cache.get("last_checked", 0) > 60):
            r = session.get(f"https://kick.com/api/v2/channels/{ch_slug}", timeout_seconds=6)
            if r.status_code == 200:
                d = r.json()
                livestream = d.get("livestream")
                is_live = livestream is not None
                is_slots = False
                subcat_name = "Offline"
                if is_live and livestream:
                    for c in livestream.get("categories", []):
                        slug = (c.get("slug") or "").lower()
                        name = (c.get("name") or "").lower()
                        parent = (c.get("category", {}).get("name") or "").lower()
                        if "slot" in slug or "slot" in name or "casino" in name or "gambl" in parent:
                            is_slots = True
                            subcat_name = c.get("name") or "Slots & Casino"
                            break

                ch_id = d.get("id")
                room_id = d.get("chatroom", {}).get("id")
                cache = {
                    "id": ch_id, 
                    "chatroom_id": room_id, 
                    "is_live": is_live and is_slots,
                    "category": subcat_name,
                    "last_checked": now
                }
                radar_state["channel_cache"][ch_slug] = cache
            else:
                return

        if not cache.get("is_live", True) or not ch_id:
            return

        # Fetch latest 25 messages from the live chatroom
        mr = session.get(f"https://kick.com/api/v2/channels/{ch_id}/messages", timeout_seconds=6)
        if mr.status_code != 200:
            return
        msgs = mr.json().get("data", {}).get("messages", [])

        for m in reversed(msgs):
            mid = m.get("id")
            if mid in radar_state["processed_msg_ids"]:
                continue
            radar_state["processed_msg_ids"].add(mid)
            if len(radar_state["processed_msg_ids"]) > 1000:
                first = next(iter(radar_state["processed_msg_ids"]))
                radar_state["processed_msg_ids"].remove(first)

            sender = (m.get("sender", {}).get("username") or "").lower()
            content = m.get("content") or ""
            is_botrix = "botrix" in sender or sender == "bot_rix"
            is_streamer = sender == ch_slug.lower()

            if is_botrix or is_streamer:
                kw = None
                for pat in GIVEAWAY_PATTERNS:
                    m_kw = pat.search(content)
                    if m_kw:
                        kw = m_kw.group(1).strip()
                        break

                if kw and len(kw) <= 40:
                    chatroom_id = cache.get("chatroom_id")
                    if not chatroom_id:
                        # Fetch chatroom ID on-demand for instant entry
                        try:
                            cr = session.get(f"https://kick.com/api/v2/channels/{ch_slug}", timeout_seconds=4)
                            if cr.status_code == 200:
                                chatroom_id = cr.json().get("chatroom", {}).get("id")
                                cache["chatroom_id"] = chatroom_id
                                radar_state["channel_cache"][ch_slug] = cache
                        except Exception:
                            pass

                    evt = {
                        "id": f"{ch_slug}_{mid}",
                        "channel": ch_slug,
                        "chatroom_id": chatroom_id,
                        "channel_id": ch_id,
                        "keyword": kw,
                        "sender": sender,
                        "content": content,
                        "time": time.strftime("%H:%M:%S"),
                        "timestamp": int(now)
                    }
                    radar_state["events"].insert(0, evt)
                    radar_state["events"] = radar_state["events"][:30]
                    print(f"[RADAR] 🎁 SLOTS GIVEAWAY DETECTED on #{ch_slug}! Keyword: '{kw}' from @{sender}")

                    if radar_state.get("auto_enter") and chatroom_id:
                        try:
                            if os.path.exists(TOKENS_FILE):
                                with open(TOKENS_FILE, "r", encoding="utf-8") as f:
                                    tks = [line.strip() for line in f if line.strip()]
                                if tks:
                                    print(f"[RADAR] Auto-entering on #{ch_slug} with {len(tks)} tokens for '{kw}'...")
                                    threading.Thread(
                                        target=auto_enter_giveaway,
                                        args=(chatroom_id, ch_slug, kw, tks),
                                        daemon=True
                                    ).start()
                        except Exception as e:
                            print(f"[RADAR] Auto-enter error: {e}")
    except Exception:
        pass

def auto_enter_giveaway(chatroom_id, channel, keyword, tokens):
    content = format_kick_message(keyword, channel)
    for i, token in enumerate(tokens):
        bare = token.strip()
        if ":" in bare: bare = bare.split(":")[-1]
        if "|" in bare: bare = bare.split("|")[-1]
        s = make_session(token=bare, channel=channel)
        s.headers["content-type"] = "application/json"
        try:
            s.post(f"https://kick.com/api/v2/messages/send/{chatroom_id}",
                   json={"content": content, "type": "message"}, timeout_seconds=8)
        except Exception:
            pass
        time.sleep(0.35)

def radar_background_loop():
    from concurrent.futures import ThreadPoolExecutor
    session = make_session()
    while True:
        try:
            if radar_state.get("enabled"):
                targets = []
                if radar_state.get("auto_discover_slots", True):
                    # All discovered live Slots & Casino streamers
                    live_list = list(radar_state.get("live_slots_streamers", {}).values())
                    # Sort by viewer count desc so high-traffic channels are scanned first
                    live_list.sort(key=lambda x: x.get("viewers", 0), reverse=True)
                    targets.extend(live_list)

                    # Also include any custom watchlist streams not already in discovered list
                    live_slugs = {s["slug"] for s in live_list}
                    for custom_ch in radar_state.get("watchlist", []):
                        if custom_ch.lower() not in live_slugs:
                            targets.append({"slug": custom_ch.lower()})
                else:
                    for ch in radar_state.get("watchlist", []):
                        targets.append({"slug": ch.lower()})

                if targets:
                    with ThreadPoolExecutor(max_workers=12) as ex:
                        list(ex.map(lambda t: scan_channel_for_giveaway(t, session), targets))
            time.sleep(1.5)
        except Exception:
            time.sleep(2.5)

threading.Thread(target=radar_background_loop, daemon=True).start()

@app.get("/api/radar/status")
async def get_radar_status():
    discovered = list(radar_state.get("live_slots_streamers", {}).values())
    discovered.sort(key=lambda x: x.get("viewers", 0), reverse=True)
    live_channels = [
        {
            "slug": s.get("slug"),
            "username": s.get("username"),
            "viewers": s.get("viewers", 0),
            "title": s.get("title", ""),
            "is_live": True
        }
        for s in discovered
    ]
    return {
        "ok": True,
        "enabled": radar_state["enabled"],
        "auto_enter": radar_state["auto_enter"],
        "auto_discover_slots": radar_state.get("auto_discover_slots", True),
        "live_slots_count": len(discovered),
        "watchlist": radar_state["watchlist"],
        "live_channels": live_channels,
        "events": radar_state["events"][:15],
        "last_discovery_time": radar_state.get("last_discovery_time", 0)
    }

@app.post("/api/radar/refresh_slots")
async def refresh_slots_streamers():
    try:
        discovered = fetch_live_slots_streamers(max_streams=250)
        now = time.time()
        st_map = {}
        for s in discovered:
            slug = s["slug"]
            st_map[slug] = s
            existing_cache = radar_state["channel_cache"].get(slug, {})
            radar_state["channel_cache"][slug] = {
                "id": s["channel_id"],
                "chatroom_id": existing_cache.get("chatroom_id"),
                "is_live": True,
                "category": "Slots & Casino",
                "viewers": s["viewers"],
                "title": s["title"],
                "last_checked": now
            }
        radar_state["live_slots_streamers"] = st_map
        radar_state["last_discovery_time"] = int(now)
        return {"ok": True, "count": len(st_map)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

class RadarWatchlistRequest(BaseModel):
    action: str  # "add", "remove", "set"
    channel: Optional[str] = None
    watchlist: Optional[List[str]] = None

@app.post("/api/radar/watchlist")
async def update_radar_watchlist(req: RadarWatchlistRequest):
    if req.action == "add" and req.channel:
        raw = req.channel.replace(",", " ").replace("\n", " ")
        names = [x.strip().lower().replace("#", "") for x in raw.split() if x.strip()]
        for ch in names:
            if ch and ch not in radar_state["watchlist"]:
                radar_state["watchlist"].append(ch)
        save_radar_state()
    elif req.action == "remove" and req.channel:
        ch = req.channel.strip().lower().replace("#", "")
        if ch in radar_state["watchlist"]:
            radar_state["watchlist"].remove(ch)
            save_radar_state()
    elif req.action == "set" and req.watchlist is not None:
        radar_state["watchlist"] = [c.strip().lower().replace("#", "") for c in req.watchlist if c.strip()]
        save_radar_state()
    return {"ok": True, "watchlist": radar_state["watchlist"]}

class RadarToggleRequest(BaseModel):
    enabled: Optional[bool] = None
    auto_enter: Optional[bool] = None
    auto_discover_slots: Optional[bool] = None

@app.post("/api/radar/toggle")
async def toggle_radar(req: RadarToggleRequest):
    if req.enabled is not None:
        radar_state["enabled"] = req.enabled
    if req.auto_enter is not None:
        radar_state["auto_enter"] = req.auto_enter
    if req.auto_discover_slots is not None:
        radar_state["auto_discover_slots"] = req.auto_discover_slots
    save_radar_state()
    return {
        "ok": True, 
        "enabled": radar_state["enabled"], 
        "auto_enter": radar_state["auto_enter"],
        "auto_discover_slots": radar_state.get("auto_discover_slots", True)
    }

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

