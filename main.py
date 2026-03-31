"""
直播弹幕下载 API
FastAPI + yt-dlp

Deploy on Render.com:
- Runtime: Python 3.11
- Build command: pip install -r requirements.txt
- Start command: uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import os
import json
import re
import tempfile
import subprocess
from pathlib import Path
from typing import Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Chat Analyzer API", version="1.0.0")

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    url: str


def extract_video_id(url: str) -> Optional[str]:
    patterns = [
        r"[?&]v=([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/live/([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def parse_ndjson_file(filepath: str):
    messages = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            replay = ev.get("replayChatItemAction", {})
            offset_ms = int(replay.get("videoOffsetTimeMsec", 0) or 0)

            for action in replay.get("actions", []):
                item = action.get("addChatItemAction", {}).get("item", {})

                r = item.get("liveChatTextMessageRenderer")
                if r:
                    text = "".join(
                        run.get("text", "")
                        or (run.get("emoji", {}).get("shortcuts", [""])[0]
                            if run.get("emoji", {}).get("shortcuts") else "")
                        for run in r.get("message", {}).get("runs", [])
                    ).strip()
                    author = r.get("authorName", {}).get("simpleText", "")
                    if text:
                        messages.append({"text": text, "author": author, "timeMs": offset_ms, "type": "chat", "amount": 0})
                    continue

                sc = item.get("liveChatPaidMessageRenderer")
                if sc:
                    runs = sc.get("message", {}).get("runs", [])
                    text = "".join(run.get("text", "") for run in runs).strip()
                    author = sc.get("authorName", {}).get("simpleText", "")
                    amt_str = sc.get("purchaseAmountText", {}).get("simpleText", "0")
                    amount = int(re.sub(r"[^\d]", "", amt_str) or 0)
                    messages.append({"text": text or "[SuperChat]", "author": author, "timeMs": offset_ms, "type": "superchat", "amount": amount})
                    continue

                mem = item.get("liveChatMembershipItemRenderer")
                if mem:
                    runs = mem.get("headerSubtext", {}).get("runs", [])
                    text = "".join(run.get("text", "") for run in runs).strip()
                    author = mem.get("authorName", {}).get("simpleText", "")
                    messages.append({"text": text or "[新メンバー]", "author": author, "timeMs": offset_ms, "type": "member", "amount": 0})

    return sorted(messages, key=lambda m: m["timeMs"])


@app.get("/")
def health():
    return {"status": "ok", "service": "chat-analyzer-api"}


@app.post("/api/chat")
def download_chat(req: AnalyzeRequest):
    video_id = extract_video_id(req.url)
    if not video_id:
        raise HTTPException(status_code=400, detail="无法解析 YouTube URL，请检查格式")

    with tempfile.TemporaryDirectory() as tmpdir:
        output_template = str(Path(tmpdir) / "chat")
        try:
            result = subprocess.run(
                ["yt-dlp", "--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", output_template, req.url],
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=408, detail="下载超时，该视频弹幕可能太多")

        chat_files = list(Path(tmpdir).glob("*.live_chat.json"))
        if not chat_files:
            raise HTTPException(status_code=404, detail="未找到弹幕数据。该视频可能没有直播弹幕回放")

        messages = parse_ndjson_file(str(chat_files[0]))
        if not messages:
            raise HTTPException(status_code=404, detail="弹幕文件为空")

        title_match = re.search(r"\[download\] Destination: (.+?)\.live_chat", result.stdout)
        raw_title = title_match.group(1).split("/")[-1] if title_match else f"chat_{video_id}"

        return {"videoId": video_id, "title": raw_title, "messageCount": len(messages), "messages": messages}
