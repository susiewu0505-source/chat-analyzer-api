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
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Chat Analyzer API", version="1.0.0")

# Allow your Netlify domain + localhost for dev
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


def parse_ndjson_file(filepath: str) -> list[dict]:
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

                # Regular chat
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
                        messages.append({
                            "text": text,
                            "author": author,
                            "timeMs": offset_ms,
                            "type": "chat",
                            "amount": 0,
                        })
                    continue

                # Superchat
                sc = item.get("liveChatPaidMessageRenderer")
                if sc:
                    runs = sc.get("message", {}).get("runs", [])
                    text = "".join(run.get("text", "") for run in runs).strip()
                    author = sc.get("authorName", {}).get("simpleText", "")
                    amt_str = sc.get("purchaseAmountText", {}).get("simpleText", "0")
                    amount = int(re.sub(r"[^\d]", "", amt_str) or 0)
                    messages.append({
                        "text": text or "[SuperChat]",
                        "author": author,
                        "timeMs": offset_ms,
                        "type": "superchat",
                        "amount": amount,
                    })
                    continue

                # Membership
                mem = item.get("liveChatMembershipItemRenderer")
                if mem:
                    runs = mem.get("headerSubtext", {}).get("runs", [])
                    text = "".join(run.get("text", "") for run in runs).strip()
                    author = mem.get("authorName", {}).get("simpleText", "")
                    messages.append({
                        "text": text or "[新メンバー]",
                        "author": author,
                        "timeMs": offset_ms,
                        "type": "member",
                        "amount": 0,
                    })

    return sorted(messages, key=lambda m: m["timeMs"])


@app.get("/")
def health():
    return {"status": "ok", "service": "chat-analyzer-api"}


@app.get("/api/info")
def info(url: str):
    """Get video title and basic info without downloading chat"""
    video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(400, "无法解析 YouTube URL")

    try:
        result = subprocess.run(
            ["yt-dlp", "--skip-download", "--print", "%(title)s\t%(duration)s\t%(upload_date)s", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise HTTPException(400, "无法获取视频信息，请确认 URL 正确且视频有直播弹幕回放")

        parts = result.stdout.strip().split("\t")
        title = parts[0] if parts else "Unknown"
        duration = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        date = parts[2] if len(parts) > 2 else ""

        return {
            "videoId": video_id,
            "title": title,
            "duration": duration,
            "date": date,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "获取视频信息超时")


@app.post("/api/chat")
def download_chat(req: AnalyzeRequest):
    """Download and parse live chat from YouTube URL"""
    video_id = extract_video_id(req.url)
    if not video_id:
        raise HTTPException(400, "无法解析 YouTube URL，请检查格式")

    with tempfile.TemporaryDirectory() as tmpdir:
        output_template = str(Path(tmpdir) / "chat")

        # Download live chat only
        try:
            result = subprocess.run(
                [
                    "yt-dlp",
                    "--skip-download",
                    "--write-subs",
                    "--sub-langs", "live_chat",
                    "-o", output_template,
                    req.url,
                ],
                capture_output=True,
                text=True,
                timeout=120,  # 2 min max
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(408, "下载超时（超过2分钟），该视频弹幕可能太多或网络较慢")

        # Find the downloaded file
        chat_files = list(Path(tmpdir).glob("*.live_chat.json"))
        if not chat_files:
            raise HTTPException(404, "未找到弹幕数据。可能原因：① 该视频没有直播弹幕回放 ② 视频是私密/受限的 ③ 不是直播视频")

        # Parse messages
        messages = parse_ndjson_file(str(chat_files[0]))
        if not messages:
            raise HTTPException(404, "弹幕文件为空")

        # Get video title from yt-dlp output or filename
        title_match = re.search(r"\[download\] Destination: (.+?)\.live_chat", result.stdout)
        raw_title = title_match.group(1).split("/")[-1] if title_match else f"chat_{video_id}"

        return {
            "videoId": video_id,
            "title": raw_title,
            "messageCount": len(messages),
            "messages": messages,
        }

