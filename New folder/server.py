# -*- coding: utf-8 -*-
"""
Kick Chat Panel — Railway Edition
INSTALL:  pip install -r requirements.txt
RUN:      python server.py
"""

import os, json, time, random, threading
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
        bare = token.split(":")[-1] if ":" in token else token
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
username_cache = {}

def lookup_username(token: str, proxy: str = None) -> dict:
    bare = token.split(":")[-1] if ":" in token else token
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
    target_id = get_channel_user_id(req.target_channel)
    if not target_id:
        return {"ok": False, "error": f"Could not find channel '{req.target_channel}'"}

    results = []

    def do_follow(token, idx):
        bare = token.split(":")[-1] if ":" in token else token
        session = make_session(token=bare, channel=req.target_channel)
        try:
            session.get(f"https://kick.com/{req.target_channel}", timeout_seconds=10)
            xsrf_cookie = session.cookies.get("XSRF-TOKEN")
            if xsrf_cookie:
                import urllib.parse
                session.headers["X-Xsrf-Token"] = urllib.parse.unquote(xsrf_cookie)
            r = session.post(f"https://kick.com/api/v2/channels/{target_id}/follow", json={}, timeout_seconds=10)
            ok = r.status_code in (200, 201, 204)
            status = "followed" if ok else f"HTTP {r.status_code}"
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


# Stealth follow not available on Railway (no browser) — return helpful error
@app.post("/api/follow_stealth")
async def follow_stealth(req: MassFollowRequest):
    return {"ok": False, "error": "Stealth follow requires a local browser — use Mass Follow instead"}


@app.post("/api/check_follows")
async def check_follows(req: CheckFollowsRequest):
    if not req.tokens: return {"ok": False, "error": "No tokens provided"}
    if not req.target_id: return {"ok": False, "error": "target_id missing"}
    results = []
    for i, token in enumerate(req.tokens):
        bare = token.split(":")[-1] if ":" in token else token
        session = make_session(token=bare)
        try:
            r = session.get("https://kick.com/api/v2/channels/followed", timeout_seconds=10)
            if r.status_code == 200:
                followed_list = r.json()
                target_id_str = str(req.target_id)
                is_following = isinstance(followed_list, list) and any(str(c.get("id")) == target_id_str for c in followed_list)
                results.append({"token": token, "following": is_following})
            else:
                r2 = session.get(f"https://kick.com/api/v2/channels/{req.target_id}", timeout_seconds=8)
                following = r2.status_code == 200 and r2.json().get("following") is True
                results.append({"token": token, "following": following})
        except Exception as e:
            results.append({"token": token, "following": False, "error": str(e)})
        if (i + 1) % 5 == 0: time.sleep(0.1)
    return {"ok": True, "results": results}


@app.post("/api/send")
async def send_message(req: SendRequest):
    bare = req.token.split(":")[-1] if ":" in req.token else req.token
    session = make_session(token=bare, channel=req.channel)
    session.headers["content-type"] = "application/json"
    try:
        r = session.post(f"https://kick.com/api/v2/messages/send/{req.chatroom_id}",
                         json={"content": req.message, "type": "message"}, timeout_seconds=10)
        if r.status_code in (200, 201): return {"ok": True}
        if r.status_code == 429: return {"ok": False, "error": "Rate limited"}
        if r.status_code == 401: return {"ok": False, "error": "Token invalid"}
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
        bare = token.split(":")[-1] if ":" in token else token
        session = make_session(token=bare, channel=req.channel)
        session.headers["content-type"] = "application/json"
        try:
            r = session.post(f"https://kick.com/api/v2/messages/send/{req.chatroom_id}",
                             json={"content": message, "type": "message"}, timeout_seconds=10)
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
        return {"ok": True, "tokens": []}
    with open(TOKENS_FILE, "r", encoding="utf-8") as f:
        tokens = [line.strip() for line in f if line.strip()]
    return {"ok": True, "tokens": tokens}

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"\n  Kick Chat Panel — Railway Edition")
    print(f"  Running on port {port}\n")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
