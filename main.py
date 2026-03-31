import os, json, re, sys, tempfile, subprocess
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET","POST"], allow_headers=["*"])

class AnalyzeRequest(BaseModel):
    url: str

def extract_video_id(url):
    for pat in [r"[?&]v=([a-zA-Z0-9_-]{11})", r"youtu\.be/([a-zA-Z0-9_-]{11})"]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None

def parse_ndjson(filepath):
    msgs = []
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            replay = ev.get("replayChatItemAction", {})
            ms = int(replay.get("videoOffsetTimeMsec", 0) or 0)
            for action in replay.get("actions", []):
                item = action.get("addChatItemAction", {}).get("item", {})
                r = item.get("liveChatTextMessageRenderer")
                if r:
                    runs = r.get("message", {}).get("runs", [])
                    text = "".join(run.get("text","") or (run.get("emoji",{}).get("shortcuts",[""])[0] if run.get("emoji",{}).get("shortcuts") else "") for run in runs).strip()
                    if text:
                        msgs.append({"text": text, "author": r.get("authorName",{}).get("simpleText",""), "timeMs": ms, "type": "chat", "amount": 0})
    return sorted(msgs, key=lambda m: m["timeMs"])

@app.get("/")
def health():
    return {"status": "ok"}

@app.post("/api/chat")
def download_chat(req: AnalyzeRequest):
    vid = extract_video_id(req.url)
    if not vid:
        raise HTTPException(status_code=400, detail="无法解析YouTube URL")
    with tempfile.TemporaryDirectory() as tmp:
        out = str(Path(tmp) / "chat")
        try:
            subprocess.run(
                [sys.executable, "-m", "yt_dlp", "--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", out, req.url],
                capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=408, detail="下载超时")
        files = list(Path(tmp).glob("*.live_chat.json"))
        if not files:
            raise HTTPException(status_code=404, detail="未找到弹幕数据")
        msgs = parse_ndjson(str(files[0]))
        if not msgs:
            raise HTTPException(status_code=404, detail="弹幕为空")
        return {"videoId": vid, "title": files[0].name.replace(".live_chat.json",""), "messageCount": len(msgs), "messages": msgs}
