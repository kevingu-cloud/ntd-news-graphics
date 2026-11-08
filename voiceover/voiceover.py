#!/usr/bin/env python3
"""口播稿 → AI 配音（MP3）＋ 字幕（SRT）。

每一行是一段，每段分開生成並快取：改了哪一段，就只重新生成那一段。
支援三種配音引擎：edge（免費，只適合測試）、azure（Azure 官方服務）、elevenlabs。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path
from xml.sax.saxutils import escape

SAMPLE_RATE = 44100
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_VOICES = {
    "edge": "zh-CN-YunxiNeural",
    "azure": "zh-CN-YunxiNeural",
}
# ElevenLabs v3 的情緒標籤，例如 [excited]、[sighs]；只送給 v3，字幕裡自動拿掉
TAG_RE = re.compile(r"\[[^\[\]]{1,40}\]\s*")
SENTENCE_END = "。！？!?；;"
CLAUSE_END = "，,、：:"
PUNCT = set(SENTENCE_END + CLAUSE_END + "　 「」『』（）()《》〈〉“”‘’\"'…—-·")


# ---------- 設定與文字前處理 ----------

def load_dotenv() -> None:
    """讀取 .env（目前資料夾或本工具資料夾），已存在的環境變數不覆蓋。"""
    for path in (Path.cwd() / ".env", SCRIPT_DIR / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_lexicon(path: Path | None) -> list[tuple[str, str]]:
    """讀音修正表：每行「原文=實際要念的寫法」，# 開頭是註解。"""
    if not path or not path.is_file():
        return []
    rules = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        src, dst = line.split("=", 1)
        if src.strip():
            rules.append((src.strip(), dst.strip()))
    # 長的詞先替換，避免被短的詞搶先
    return sorted(rules, key=lambda r: -len(r[0]))


def apply_lexicon(text: str, rules: list[tuple[str, str]]) -> str:
    for src, dst in rules:
        text = text.replace(src, dst)
    return text


def to_mainland(text: str) -> str:
    """繁體（台灣用語）→ 簡體（大陸用語），例如 軟體→软件、影片→视频。"""
    try:
        from opencc import OpenCC
    except ImportError:
        sys.exit("需要 opencc：pip install -r requirements.txt")
    return OpenCC("tw2sp").convert(text)


# ---------- 配音引擎 ----------
# 每個引擎回傳 (音訊 bytes, 逐字時間或 None)。
# 逐字時間格式：{"characters": [...], "start": [...], "end": [...]}（秒）

def speed_to_percent(speed: float) -> str:
    return f"{round((speed - 1) * 100):+d}%"


async def _edge_tts(text: str, voice: str, speed: float) -> bytes:
    import edge_tts
    import edge_tts.communicate as communicate

    # 公司網路或代理伺服器有自己的憑證時，用 SSL_CERT_FILE 指定的憑證
    cafile = os.environ.get("SSL_CERT_FILE")
    if cafile and hasattr(communicate, "_SSL_CTX"):
        communicate._SSL_CTX = ssl.create_default_context(cafile=cafile)

    audio = bytearray()
    stream = edge_tts.Communicate(text, voice, rate=speed_to_percent(speed)).stream()
    async for chunk in stream:
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return bytes(audio)


def synth_edge(text: str, args) -> tuple[bytes, None]:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        sys.exit("需要 edge-tts：pip install -r requirements.txt")
    return asyncio.run(_edge_tts(text, args.voice, args.speed)), None


def http_post(url: str, headers: dict, body: bytes) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:500]
        sys.exit(f"配音服務回傳錯誤 {err.code}：{detail}")


def synth_azure(text: str, args) -> tuple[bytes, None]:
    key = os.environ.get("AZURE_SPEECH_KEY")
    region = os.environ.get("AZURE_SPEECH_REGION")
    if not key or not region:
        sys.exit("請在 .env 設定 AZURE_SPEECH_KEY 和 AZURE_SPEECH_REGION")
    content = f'<prosody rate="{speed_to_percent(args.speed)}">{escape(text)}</prosody>'
    if args.style:
        content = f'<mstts:express-as style="{escape(args.style)}">{content}</mstts:express-as>'
    ssml = (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="zh-CN">'
        f'<voice name="{escape(args.voice)}">{content}</voice></speak>'
    )
    audio = http_post(
        f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
        {
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
            "User-Agent": "voiceover-tool",
        },
        ssml.encode("utf-8"),
    )
    return audio, None


def synth_elevenlabs(text: str, args) -> tuple[bytes, dict | None]:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        sys.exit("請在 .env 設定 ELEVENLABS_API_KEY")
    if not args.voice:
        sys.exit("ElevenLabs 要指定 --voice（聲音 ID），可用 --list-voices 查詢")
    body = {
        "text": text,
        "model_id": args.model,
        "voice_settings": {
            "stability": args.stability,
            "similarity_boost": 0.75,
            "style": args.style_strength,
            "use_speaker_boost": True,
            "speed": args.speed,
        },
    }
    raw = http_post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{args.voice}/with-timestamps"
        "?output_format=mp3_44100_128",
        {"xi-api-key": key, "Content-Type": "application/json"},
        json.dumps(body, ensure_ascii=False).encode("utf-8"),
    )
    data = json.loads(raw)
    alignment = data.get("alignment")
    timing = None
    if alignment:
        timing = {
            "characters": alignment["characters"],
            "start": alignment["character_start_times_seconds"],
            "end": alignment["character_end_times_seconds"],
        }
    return base64.b64decode(data["audio_base64"]), timing


ENGINES = {"edge": synth_edge, "azure": synth_azure, "elevenlabs": synth_elevenlabs}


def list_voices(engine: str) -> None:
    if engine == "elevenlabs":
        key = os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            sys.exit("請在 .env 設定 ELEVENLABS_API_KEY")
        request = urllib.request.Request(
            "https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            voices = json.loads(response.read())["voices"]
        for voice in voices:
            labels = ", ".join(f"{k}={v}" for k, v in (voice.get("labels") or {}).items())
            print(f"{voice['voice_id']}  {voice['name']}  {labels}")
        return
    import edge_tts

    async def run():
        for voice in await edge_tts.list_voices():
            if voice["Locale"].startswith("zh-CN"):
                print(f"{voice['ShortName']}  {voice['Gender']}")

    asyncio.run(run())


# ---------- 音訊處理 ----------

def ffmpeg(*args: str, stdin: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
            input=stdin, capture_output=True, check=True,
        )
    except FileNotFoundError:
        sys.exit("找不到 ffmpeg，請先安裝（Mac：brew install ffmpeg；Windows：winget install ffmpeg）")
    except subprocess.CalledProcessError as err:
        sys.exit(f"ffmpeg 失敗：{err.stderr.decode('utf-8', 'replace')}")
    return result.stdout


# 輕度壓縮讓人聲更集中（廣播常用），再標準化音量
COMPRESSOR = "acompressor=threshold=0.1:ratio=3:attack=5:release=150"
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"


def loudnorm_filter(path: Path) -> str:
    """兩階段音量標準化到 YouTube 標準 -14 LUFS：先量測，再依量測值精準調整。"""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path),
             "-af", f"{COMPRESSOR},{LOUDNORM}:print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        )
        stats = json.loads(result.stderr[result.stderr.rindex("{"):])
        return (
            f"{COMPRESSOR},{LOUDNORM}:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
            f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
            f":offset={stats['target_offset']}:linear=true"
        )
    except (subprocess.CalledProcessError, ValueError, KeyError):
        return f"{COMPRESSOR},{LOUDNORM}"  # 量測失敗時退回單次標準化


def to_pcm(mp3_path: Path) -> bytes:
    """解碼成 44.1kHz 單聲道 16-bit PCM，方便精準計算時間與接合。"""
    return ffmpeg("-i", str(mp3_path), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-")


# ---------- 字幕 ----------

def visible_len(text: str) -> int:
    return sum(1 for ch in text if ch not in PUNCT)


def split_cues(text: str, max_chars: int) -> list[tuple[int, int]]:
    """把一段文字切成字幕句，回傳每句在原文中的 (起, 迄) 位置。"""
    sentences, start = [], 0
    for i, ch in enumerate(text):
        if ch in SENTENCE_END:
            sentences.append((start, i + 1))
            start = i + 1
    if start < len(text):
        sentences.append((start, len(text)))

    cues = []
    for s_start, s_end in sentences:
        if visible_len(text[s_start:s_end]) <= max_chars:
            cues.append((s_start, s_end))
            continue
        clauses, c_start = [], s_start
        for i in range(s_start, s_end):
            if text[i] in CLAUSE_END:
                clauses.append((c_start, i + 1))
                c_start = i + 1
        if c_start < s_end:
            clauses.append((c_start, s_end))
        cur_start = cur_end = None
        for c_start, c_end in clauses:
            if cur_start is None:
                cur_start, cur_end = c_start, c_end
            elif visible_len(text[cur_start:c_end]) <= max_chars:
                cur_end = c_end
            else:
                cues.append((cur_start, cur_end))
                cur_start, cur_end = c_start, c_end
        if cur_start is not None:
            cues.append((cur_start, cur_end))

    # 單一子句仍然太長時，硬切
    result = []
    for c_start, c_end in cues:
        while visible_len(text[c_start:c_end]) > max_chars:
            count, i = 0, c_start
            while i < c_end and count < max_chars:
                if text[i] not in PUNCT:
                    count += 1
                i += 1
            result.append((c_start, i))
            c_start = i
        result.append((c_start, c_end))
    return [(a, b) for a, b in result if visible_len(text[a:b]) > 0]


def clean_cue(text: str) -> str:
    return text.strip().strip("，,。、；;：: 　")


def supports_tags(args) -> bool:
    return args.engine == "elevenlabs" and args.model.startswith("eleven_v3")


def align_to_display(spoken: str, display: str, timing: dict | None) -> dict | None:
    """把配音文字（可能含情緒標籤）的逐字時間，對應回字幕文字。對不上就回傳 None。"""
    if not timing or len(timing["characters"]) != len(spoken):
        return None
    in_tag = [False] * len(spoken)
    for match in TAG_RE.finditer(spoken):
        for i in range(match.start(), match.end()):
            in_tag[i] = True
    kept = [i for i in range(len(spoken)) if not in_tag[i]]
    if "".join(spoken[i] for i in kept) != display:
        return None
    return {
        "characters": [spoken[i] for i in kept],
        "start": [timing["start"][i] for i in kept],
        "end": [timing["end"][i] for i in kept],
    }


def cue_times(text: str, cues, duration: float, timing: dict | None):
    """有逐字時間就用精準時間，否則依字數比例分配。"""
    if timing and len(timing["characters"]) == len(text):
        times = []
        for a, b in cues:
            idx = [i for i in range(a, b) if text[i] not in PUNCT]
            times.append((timing["start"][idx[0]], timing["end"][idx[-1]]))
        return times
    total = max(visible_len(text), 1)
    times, elapsed = [], 0
    for a, b in cues:
        n = visible_len(text[a:b])
        times.append((duration * elapsed / total, duration * (elapsed + n) / total))
        elapsed += n
    return times


def srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------- 主流程 ----------

def main() -> None:
    parser = argparse.ArgumentParser(description="口播稿 → AI 配音 MP3 ＋ SRT 字幕")
    parser.add_argument("script", nargs="?", help="口播稿文字檔（UTF-8，每一行是一段）")
    parser.add_argument("--engine", choices=ENGINES, default="edge")
    parser.add_argument("--voice", help="聲音名稱或 ID")
    parser.add_argument("--model", default="eleven_multilingual_v2", help="ElevenLabs 模型")
    parser.add_argument("--speed", type=float, default=1.0, help="語速，1.0 為正常")
    parser.add_argument("--style", help="Azure 說話風格，例如 newscast、narration-professional")
    parser.add_argument("--stability", type=float, default=0.5,
                        help="ElevenLabs 穩定度 0～1，低一點情緒較多；v3 只接受 0（創意）、0.5（自然）、1（穩定）")
    parser.add_argument("--style-strength", type=float, default=0.0,
                        help="ElevenLabs 風格誇張度 0～1，高一點更有表現力（v2 有效）")
    parser.add_argument("--t2s", action="store_true", help="配音前把繁體（台灣用語）轉成簡體（大陸用語）")
    parser.add_argument("--lexicon", type=Path, default=SCRIPT_DIR / "lexicon.txt", help="讀音修正表")
    parser.add_argument("--gap", type=float, default=0.4, help="段落之間的停頓秒數")
    parser.add_argument("--max-chars", type=int, default=18, help="每句字幕最多幾個字")
    parser.add_argument("-o", "--out", type=Path, help="輸出資料夾")
    parser.add_argument("--list-voices", action="store_true", help="列出可用的聲音")
    args = parser.parse_args()

    load_dotenv()
    if args.list_voices:
        list_voices(args.engine)
        return
    if not args.script:
        parser.error("請指定口播稿檔案")
    args.voice = args.voice or DEFAULT_VOICES.get(args.engine)

    script_path = Path(args.script)
    paragraphs = [
        line.strip()
        for line in script_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not paragraphs:
        sys.exit("口播稿是空的")

    out_dir = args.out or SCRIPT_DIR / "output" / script_path.stem
    seg_dir = out_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    lexicon = load_lexicon(args.lexicon)
    synth = ENGINES[args.engine]

    pcm_parts, srt_lines, cursor, cue_no = [], [], 0.0, 0
    gap_pcm = b"\x00\x00" * round(args.gap * SAMPLE_RATE)

    for index, line in enumerate(paragraphs, 1):
        display = TAG_RE.sub("", line).strip()
        spoken = line if supports_tags(args) else display
        spoken = to_mainland(spoken) if args.t2s else spoken
        spoken = apply_lexicon(spoken, lexicon)
        fingerprint = json.dumps(
            [args.engine, args.voice, args.model, args.speed, args.style, args.stability,
             args.style_strength, spoken],
            ensure_ascii=False,
        )
        digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:8]
        mp3_path = seg_dir / f"{index:03d}_{digest}.mp3"
        timing_path = mp3_path.with_suffix(".json")

        if mp3_path.exists():
            print(f"[{index}/{len(paragraphs)}] 沿用已生成的音檔")
            timing = json.loads(timing_path.read_text()) if timing_path.exists() else None
        else:
            print(f"[{index}/{len(paragraphs)}] 生成中：{spoken[:30]}…")
            audio, timing = synth(spoken, args)
            mp3_path.write_bytes(audio)
            if timing:
                timing_path.write_text(json.dumps(timing, ensure_ascii=False))

        pcm = to_pcm(mp3_path)
        duration = len(pcm) / 2 / SAMPLE_RATE
        # 逐字時間只有在字幕文字和配音文字逐字對得上時才用
        aligned = align_to_display(spoken, display, timing)
        cues = split_cues(display, args.max_chars)
        for (a, b), (start, end) in zip(cues, cue_times(display, cues, duration, aligned)):
            cue_no += 1
            srt_lines.append(
                f"{cue_no}\n{srt_time(cursor + start)} --> {srt_time(cursor + end)}\n"
                f"{clean_cue(display[a:b])}\n"
            )
        if pcm_parts:
            pcm_parts.append(gap_pcm)
            cursor += args.gap
        pcm_parts.append(pcm)
        cursor += duration

    wav_path = out_dir / "voice_raw.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"".join(pcm_parts))

    mp3_out = out_dir / "voice.mp3"
    ffmpeg("-y", "-i", str(wav_path), "-af", loudnorm_filter(wav_path),
           "-ar", str(SAMPLE_RATE), "-b:a", "192k", str(mp3_out))
    wav_path.unlink()
    (out_dir / "voice.srt").write_text("\n".join(srt_lines), encoding="utf-8")

    print(f"\n完成：{mp3_out}（{cursor:.1f} 秒）")
    print(f"字幕：{out_dir / 'voice.srt'}")


if __name__ == "__main__":
    main()
