#!/usr/bin/env python3
"""
Bulk Playlist Downloader — a local playlist/channel/video downloader (macOS, Linux, Windows)

Run:    python3 bulk_playlist_downloader.py  ->  http://127.0.0.1:8090
Files:  ~/Downloads/Playlists/<playlist name>/

Built on yt-dlp, so it works with YouTube and ~1800 other sites
(Vimeo, SoundCloud, Twitch, Dailymotion, and more).

After each download the file is checked and, if needed, converted to
H.264 + AAC (mp4) so it plays everywhere — QuickTime, Telegram, iPhone, editors.
"""

import json
import re
import shutil
import subprocess
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, Response, jsonify, request

try:
    import yt_dlp
except ImportError:
    raise SystemExit("yt-dlp not found. Install it with: pip3 install yt-dlp")

PORT = 8090
SAVE_DIR = Path.home() / "Downloads" / "Playlists"
ARCHIVE_FILE = Path.home() / ".bulk_playlist_downloader_archive.txt"
FFMPEG_OK = shutil.which("ffmpeg") is not None
FFPROBE_OK = shutil.which("ffprobe") is not None
AUDIO_FORMATS = {"mp3", "m4a", "flac", "opus", "wav", "aac"}

app = Flask(__name__)
JOBS: dict[str, dict] = {}
SAVE_DIR.mkdir(parents=True, exist_ok=True)


def home_display(path: Path) -> str:
    """Show a path as ~/... so it reads the same for every user."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


# ─────────────────────────── helpers ───────────────────────────

def fmt_duration(sec) -> str:
    if not sec:
        return ""
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def clean_name(name: str) -> str:
    # Keep Latin and Cyrillic letters so titles in either script stay readable.
    cleaned = re.sub(r'[^\w\- а-яА-ЯёЁ.,()]+', '', name or '').strip()
    return cleaned[:70] or "Playlist"


def short_error(e: Exception) -> str:
    msg = str(e)
    if "Private video" in msg:
        return "Private video — pick your browser in the Cookies field"
    if "sign in" in msg.lower() or "bot" in msg.lower():
        return "Site requires sign-in — pick your browser in the Cookies field"
    if "Video unavailable" in msg:
        return "Video unavailable (removed or blocked)"
    if "ffmpeg" in msg.lower():
        return "ffmpeg problem — reinstall it: brew install ffmpeg"
    if "DRM" in msg:
        return "Video is DRM-protected — cannot be downloaded"
    return msg[:160]


def probe_codecs(path: Path):
    """Return (video_codec, audio_codec) for a file, or (None, None)."""
    if not FFPROBE_OK:
        return None, None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30).stdout
        v = a = None
        for s in json.loads(out).get("streams", []):
            if s.get("codec_type") == "video" and not v:
                v = s.get("codec_name")
            elif s.get("codec_type") == "audio" and not a:
                a = s.get("codec_name")
        return v, a
    except Exception:
        return None, None


def ensure_compatible(path: Path, item: dict) -> Path:
    """
    Guarantee H.264 + AAC in mp4. If the codecs are already fine, just swap
    the container when needed (instant). Otherwise re-encode.
    """
    if not (FFMPEG_OK and FFPROBE_OK):
        return path
    v, a = probe_codecs(path)
    if v is None:
        return path

    v_ok = v in ("h264", "avc1")
    a_ok = a in ("aac", "mp4a", None)
    if v_ok and a_ok and path.suffix == ".mp4":
        return path

    item["state"] = "converting"
    item["percent"] = 100
    target = path.with_suffix(".compat.mp4")
    cmd = ["ffmpeg", "-y", "-i", str(path),
           "-c:v", "copy" if v_ok else "libx264",
           "-c:a", "copy" if a_ok else "aac",
           "-movflags", "+faststart"]
    if not v_ok:
        cmd += ["-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    cmd.append(str(target))

    res = subprocess.run(cmd, capture_output=True)
    if res.returncode == 0 and target.exists() and target.stat().st_size > 0:
        path.unlink(missing_ok=True)
        final = path.with_suffix(".mp4")
        if final.exists() and final != target:
            final.unlink()
        target.rename(final)
        return final
    target.unlink(missing_ok=True)
    return path


def build_opts(cfg: dict, outdir: Path) -> dict:
    opts = {
        # [id] in the name lets us find the file afterwards to check codecs
        "outtmpl": str(outdir / "%(title).140s [%(id)s].%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 3,
        "overwrites": True,
        "ignoreerrors": False,
    }
    if cfg.get("browser") and cfg["browser"] != "none":
        opts["cookiesfrombrowser"] = (cfg["browser"],)
    if cfg.get("skip_downloaded"):
        opts["download_archive"] = str(ARCHIVE_FILE)

    langs = (cfg.get("subs_langs") or "").strip()
    if langs:
        opts.update(writesubtitles=True, writeautomaticsub=True,
                    subtitleslangs=[s.strip() for s in langs.split(",") if s.strip()])

    if cfg.get("mode") == "audio":
        afmt = cfg.get("audio_format", "mp3")
        afmt = afmt if afmt in AUDIO_FORMATS else "mp3"
        opts["format"] = "bestaudio/best"
        post = [{"key": "FFmpegExtractAudio",
                 "preferredcodec": afmt,
                 "preferredquality": cfg.get("bitrate", "0")}]
        if FFMPEG_OK and cfg.get("tag", True):
            opts["writethumbnail"] = True
            post.append({"key": "FFmpegMetadata"})
            if afmt in {"mp3", "m4a", "flac"}:
                post.append({"key": "EmbedThumbnail"})
        opts["postprocessors"] = post
        return opts

    # video: always try H.264+AAC first (native for Mac/Telegram)
    q = cfg.get("quality", "1080")
    height = "" if q == "best" else f"[height<={q}]"
    if FFMPEG_OK:
        if cfg.get("original"):
            opts["format"] = (f"bestvideo{height}+bestaudio"
                              f"/best{height}/best")
        else:
            opts["format"] = (
                f"bestvideo{height}[vcodec^=avc1]+bestaudio[ext=m4a]"
                f"/bestvideo{height}[vcodec^=avc1]+bestaudio"
                f"/bestvideo{height}+bestaudio"
                f"/best{height}/best"
            )
        opts["merge_output_format"] = "mp4"
        if cfg.get("tag", True):
            opts["postprocessors"] = [{"key": "FFmpegMetadata"}]
    else:
        opts["format"] = f"best{height}[ext=mp4]/best{height}/best"
    return opts


# ─────────────────────────── API ───────────────────────────

@app.get("/api/status")
def api_status():
    return jsonify({"ffmpeg": FFMPEG_OK, "save_dir": home_display(SAVE_DIR)})


@app.post("/api/info")
def api_info():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Paste a link"}), 400

    opts = {"quiet": True, "extract_flat": "in_playlist", "playlist_items": "1-2000"}
    if data.get("browser") and data["browser"] != "none":
        opts["cookiesfrombrowser"] = (data["browser"],)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": short_error(e)}), 400

    entries = info.get("entries") or ([info] if info.get("id") else [])
    title = info.get("title") or "Video"
    videos = []
    for i, e in enumerate(entries, 1):
        if not e:
            continue
        vid = e.get("id") or ""
        videos.append({
            "index": i, "id": vid,
            "url": e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "title": e.get("title") or vid,
            "duration_str": fmt_duration(e.get("duration")),
            "channel": e.get("channel") or e.get("uploader") or "",
            "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        })
    return jsonify({"playlist_title": title, "count": len(videos), "videos": videos})


@app.post("/api/download")
def api_download():
    cfg = request.get_json(force=True)
    videos = cfg.get("videos") or []
    if not videos:
        return jsonify({"error": "No videos selected"}), 400

    outdir = SAVE_DIR / clean_name(cfg.get("playlist_title"))
    outdir.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4().hex[:8]
    job = JOBS[job_id] = {
        "status": "running",
        "total": len(videos), "done": 0, "failed": 0,
        "outdir": str(outdir),
        "items": {v["id"]: {"title": v["title"], "state": "wait",
                            "percent": 0, "speed": "", "error": ""}
                  for v in videos},
    }
    lock = threading.Lock()
    parallel = max(1, min(int(cfg.get("parallel", 3)), 4))
    convert = not cfg.get("original")   # "original" = leave codecs untouched

    def find_file(vid: str):
        for f in outdir.iterdir():
            if f"[{vid}]" in f.name and f.suffix not in (".part", ".ytdl", ".webp"):
                return f
        return None

    def download_one(v):
        vid, item = v["id"], job["items"][v["id"]]
        item["state"] = "downloading"

        def hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                item["percent"] = round(got / total * 100) if total else 0
                spd = d.get("speed")
                item["speed"] = f"{spd / 1048576:.1f} MB/s" if spd else ""
            elif d.get("status") == "finished":
                item["state"], item["percent"], item["speed"] = "processing", 100, ""

        opts = build_opts(cfg, outdir)
        opts["progress_hooks"] = [hook]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                code = ydl.download([v["url"]])
            if code != 0:
                item["state"], item["error"] = "skipped", "skipped (already downloaded)"
                return
            if convert and cfg.get("mode") != "audio":
                f = find_file(vid)
                if f:
                    ensure_compatible(f, item)
            item["state"], item["percent"] = "done", 100
        except Exception as e:
            item["state"], item["error"] = "error", short_error(e)
            with lock:
                job["failed"] += 1
        finally:
            with lock:
                job["done"] += 1

    def worker():
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            list(pool.map(download_one, videos))
        for trash in ("*.part", "*.webp", "*.ytdl"):
            for f in outdir.glob(trash):
                f.unlink(missing_ok=True)
        job["status"] = "ready"

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job": job_id})


@app.get("/api/progress/<job_id>")
def api_progress(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.post("/api/open_folder")
def api_open_folder():
    path = request.get_json(force=True).get("path") or str(SAVE_DIR)
    subprocess.Popen(["open", path if Path(path).exists() else str(SAVE_DIR)])
    return jsonify({"ok": True})


# ─────────────────────────── page ───────────────────────────

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bulk Playlist Downloader</title>
<style>
:root{--bg:#101418;--panel:#171d23;--panel2:#1e262e;--line:#2a343e;--text:#e8edf2;
--dim:#8a98a6;--accent:#ff4f33;--gold:#ffb22e;--ok:#3ecf8e;--warn:#f59e0b;
--mono:"SF Mono",ui-monospace,Menlo,monospace}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,system-ui,sans-serif;padding:28px 20px 90px}
.wrap{max-width:920px;margin:0 auto}
h1{font-size:26px;margin-bottom:3px}h1 b{color:var(--accent)}
.sub{color:var(--dim);font-size:13px;margin-bottom:6px}
.savedir{font-size:12.5px;color:var(--dim);margin-bottom:18px}
.savedir b{color:var(--gold);font-family:var(--mono);font-size:12px}
.banner{display:none;background:#2d1f00;border:1px solid var(--warn);border-radius:10px;
padding:13px 16px;margin-bottom:16px;font-size:13px;line-height:1.8}
.banner.show{display:block}.banner b{color:var(--warn)}
.banner code{background:#1a1200;padding:1px 6px;border-radius:4px;font-family:var(--mono);font-size:12px;color:#fcd34d}
.bar{display:flex;gap:10px;margin-bottom:14px}
input[type=text]{flex:1;background:var(--panel);border:1px solid var(--line);border-radius:9px;
color:var(--text);padding:12px 15px;font-size:15px;outline:none}
input[type=text]:focus{border-color:var(--accent)}
button{background:var(--accent);border:0;border-radius:9px;color:#fff;font-weight:600;
font-size:14.5px;padding:12px 20px;cursor:pointer}
button:disabled{opacity:.4;cursor:default}
button.ghost{background:var(--panel2);color:var(--text);font-weight:500}
button.green{background:var(--ok);color:#052016;font-weight:700}
.settings{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:9px;
background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
.set label{display:block;font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin-bottom:4px}
select,.set input[type=text]{width:100%;background:var(--panel2);border:1px solid var(--line);
border-radius:7px;color:var(--text);padding:7px 9px;font-size:14px;outline:none}
.chk{display:flex;align-items:center;gap:7px;font-size:12.5px;padding-top:15px}
.chk input{accent-color:var(--accent)}
.hint{grid-column:1/-1;font-size:12px;color:var(--dim);line-height:1.5}
.hint b{color:var(--ok)}
.msg{font-size:14px;color:var(--gold);min-height:18px;margin-bottom:6px}
.toolbar{display:flex;align-items:center;gap:10px;margin-bottom:9px;flex-wrap:wrap}
.count{font-family:var(--mono);font-size:13px;color:var(--gold)}
.list{border:1px solid var(--line);border-radius:12px;overflow:hidden}
.vrow{display:flex;gap:11px;align-items:center;padding:9px 13px;border-bottom:1px solid var(--line);
background:var(--panel);cursor:pointer}
.vrow:last-child{border-bottom:0}
.vrow:hover{background:var(--panel2)}
.vrow.off{opacity:.32}
.vrow img{width:88px;height:50px;object-fit:cover;border-radius:5px;background:#111;flex-shrink:0}
.vrow .t{flex:1;min-width:0}
.vrow .title{font-size:13.5px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vrow .ch{font-size:12px;color:var(--dim)}
.vrow .dur{font-family:var(--mono);font-size:12px;color:var(--dim)}
.vrow .idx{font-family:var(--mono);font-size:11px;color:var(--dim);width:24px;text-align:right;flex-shrink:0}
.vrow input{width:16px;height:16px;accent-color:var(--accent);flex-shrink:0}
.hidden{display:none}
.progress{margin-top:20px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px}
.phead{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.pstat{font-size:15px;font-weight:600}
.dl-list{display:flex;flex-direction:column;gap:7px;max-height:420px;overflow-y:auto}
.dl-item{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:9px 13px}
.dl-top{display:flex;align-items:center;gap:9px;font-size:13px}
.dl-name{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500}
.dl-state{font-family:var(--mono);font-size:11.5px;color:var(--dim);white-space:nowrap}
.dl-bar{height:4px;background:#10161c;border-radius:99px;overflow:hidden;margin-top:7px}
.dl-bar i{display:block;height:100%;width:0%;border-radius:99px;background:var(--accent);transition:width .4s}
.dl-item.done .dl-bar i{background:var(--ok)}
.dl-item.conv .dl-bar i{background:var(--gold)}
.dl-item.error{border-color:#5c1f14}
.dl-item.error .dl-bar i{background:#7a2418}
.dl-err{font-size:12px;color:var(--accent);margin-top:6px;font-family:var(--mono)}
.icon{width:18px;text-align:center;flex-shrink:0}
.spin{display:inline-block;width:13px;height:13px;border:2px solid var(--dim);border-top-color:var(--accent);
border-radius:50%;animation:sp 1s linear infinite;vertical-align:-2px;margin-right:7px}
@keyframes sp{to{transform:rotate(360deg)}}
</style></head><body><div class="wrap">

<h1>Bulk Playlist <b>Downloader</b></h1>
<div class="sub">Playlists · channels · single and private videos · YouTube, Vimeo &amp; ~1800 sites</div>
<div class="savedir">Files are saved to&nbsp;<b id="savepath">…</b></div>

<div id="banner" class="banner">
 <b>⚠️ ffmpeg not installed</b> — conversion and quality above 720p are unavailable.<br>
 Terminal → <code>brew install ffmpeg</code> → restart Bulk Playlist Downloader.
</div>

<div class="bar">
  <input id="url" type="text" placeholder="Link to a playlist, channel or video…" autofocus>
  <button id="btnLoad" onclick="loadInfo()">Show videos</button>
</div>

<div class="settings">
  <div class="set"><label>Mode</label>
    <select id="mode" onchange="modeSwap()">
      <option value="video">Video + audio</option><option value="audio">Audio only</option>
    </select></div>
  <div class="set" id="s_q"><label>Quality</label>
    <select id="quality">
      <option value="best">Maximum</option><option value="2160">4K</option>
      <option value="1440">1440p</option><option value="1080" selected>1080p</option>
      <option value="720">720p</option><option value="480">480p</option>
    </select></div>
  <div class="set hidden" id="s_af"><label>Audio</label>
    <select id="aformat"><option>mp3</option><option>m4a</option><option>flac</option>
    <option>opus</option><option>wav</option><option>aac</option></select></div>
  <div class="set hidden" id="s_br"><label>Bitrate</label>
    <select id="bitrate"><option value="0">Best</option><option>320</option>
    <option>256</option><option>192</option></select></div>
  <div class="set"><label>Parallel</label>
    <select id="parallel"><option>1</option><option>2</option>
    <option selected>3</option><option>4</option></select></div>
  <div class="set"><label>Cookies (private)</label>
    <select id="browser"><option value="none">— none —</option><option>chrome</option>
    <option>safari</option><option>firefox</option><option>brave</option></select></div>
  <div class="set"><label>Subtitles</label>
    <input id="subs_langs" type="text" placeholder="en,ru"></div>
  <div class="chk"><input type="checkbox" id="original"><span>Original codecs (for archiving)</span></div>
  <div class="chk"><input type="checkbox" id="skip"><span>Don't re-download</span></div>
  <div class="hint" id="modehint"><b>✓ Plays everywhere:</b> every download is automatically saved
  in a universal format, so it opens on any device — QuickTime, Telegram, iPhone, and video editors.
  No "unsupported file" errors.</div>
</div>

<div id="msg" class="msg"></div>

<div id="picker" class="hidden">
  <div class="toolbar">
    <strong id="ptitle"></strong><span class="count" id="count"></span>
    <span style="flex:1"></span>
    <input id="filter" type="text" placeholder="Search…" style="max-width:180px;flex:none" oninput="applyFilter()">
    <button class="ghost" onclick="selAll(true)">All</button>
    <button class="ghost" onclick="selAll(false)">None</button>
    <button id="go" onclick="startDownload()">⬇ Download to folder</button>
  </div>
  <div class="list" id="list"></div>
</div>

<div id="prog" class="progress hidden">
  <div class="phead">
    <div class="pstat" id="pstat"></div>
    <button class="green hidden" id="btnOpen" onclick="openFolder()">📂 Open folder</button>
  </div>
  <div class="dl-list" id="dlist"></div>
</div>

<script>
let VIDEOS=[],TITLE="",SAVE="",OUTDIR="",FF=true;
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");

fetch("/api/status").then(r=>r.json()).then(d=>{
  FF=d.ffmpeg; SAVE=d.save_dir;
  $("savepath").textContent=d.save_dir;
  if(!FF){
    $("banner").classList.add("show");
    [...$("quality").options].forEach(o=>{
      if(["best","2160","1440","1080"].includes(o.value))o.disabled=true});
    $("quality").value="720";
  }
});

function modeSwap(){
  const a=$("mode").value==="audio";
  ["s_af","s_br"].forEach(i=>$(i).classList.toggle("hidden",!a));
  $("s_q").classList.toggle("hidden",a);
}
function cfg(){return{
  mode:$("mode").value,quality:$("quality").value,
  audio_format:$("aformat").value,bitrate:$("bitrate").value,
  parallel:$("parallel").value,browser:$("browser").value,
  original:$("original").checked,
  skip_downloaded:$("skip").checked,
  subs_langs:$("subs_langs").value.trim()};}

async function loadInfo(){
  const url=$("url").value.trim(); if(!url)return;
  $("btnLoad").disabled=true;
  $("msg").innerHTML='<span class="spin"></span>Fetching the video list…';
  $("picker").classList.add("hidden");
  try{
    const r=await fetch("/api/info",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({url,browser:$("browser").value})});
    const d=await r.json();
    if(d.error)throw new Error(d.error);
    VIDEOS=d.videos;TITLE=d.playlist_title;
    $("ptitle").textContent=TITLE;
    renderList();
    $("picker").classList.remove("hidden");
    $("msg").textContent="";
  }catch(e){$("msg").textContent="⚠ "+e.message}
  $("btnLoad").disabled=false;
}

function renderList(){
  $("list").innerHTML=VIDEOS.map(v=>`
   <div class="vrow" data-id="${v.id}" data-t="${esc(v.title.toLowerCase())}" onclick="toggle(this)">
    <input type="checkbox" checked onclick="event.stopPropagation();rowState(this)">
    <span class="idx">${v.index}</span>
    <img loading="lazy" src="${v.thumb}" onerror="this.style.visibility='hidden'">
    <div class="t"><div class="title">${esc(v.title)}</div><div class="ch">${esc(v.channel)}</div></div>
    <span class="dur">${v.duration_str}</span>
   </div>`).join("");
  updCount();
}
function toggle(row){const c=row.querySelector("input");c.checked=!c.checked;rowState(c)}
function rowState(c){c.closest(".vrow").classList.toggle("off",!c.checked);updCount()}
function selAll(on){document.querySelectorAll("#list .vrow:not([style*='none']) input")
  .forEach(c=>{c.checked=on;c.closest(".vrow").classList.toggle("off",!on)});updCount()}
function applyFilter(){const q=$("filter").value.toLowerCase();
  document.querySelectorAll("#list .vrow").forEach(r=>
    r.style.display=r.dataset.t.includes(q)?"":"none")}
function updCount(){
  const n=document.querySelectorAll("#list input:checked").length;
  $("count").textContent=`${n} / ${VIDEOS.length}`;
  $("go").disabled=!n;
}

async function startDownload(){
  const ids=new Set([...document.querySelectorAll("#list input:checked")]
    .map(c=>c.closest(".vrow").dataset.id));
  const sel=VIDEOS.filter(v=>ids.has(v.id));
  $("go").disabled=true;
  $("prog").classList.remove("hidden");
  $("btnOpen").classList.add("hidden");
  $("dlist").innerHTML="";
  $("pstat").innerHTML='<span class="spin"></span>Starting…';
  const r=await fetch("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({...cfg(),videos:sel,playlist_title:TITLE})});
  const d=await r.json();
  if(d.error){$("pstat").textContent="⚠ "+d.error;$("go").disabled=false;return}
  poll(d.job);
}

const ICONS={wait:"·",downloading:"⬇",processing:"⚙",converting:"🔄",done:"✓",skipped:"↷",error:"✗"};
const LABEL={wait:"queued",downloading:"",processing:"processing…",
             converting:"converting for compatibility…",
             done:"done",skipped:"already downloaded",error:"error"};

async function poll(job){
  let d;
  try{d=await(await fetch("/api/progress/"+job)).json()}
  catch(e){setTimeout(()=>poll(job),1500);return}
  OUTDIR=d.outdir;

  if(d.status==="ready"){
    const ok=d.total-d.failed;
    $("pstat").innerHTML=`✅ Done — ${ok} of ${d.total}`+
      (d.failed?` · <span style="color:var(--accent)">${d.failed} failed</span>`:"");
    $("btnOpen").classList.remove("hidden");
    $("go").disabled=false;
  }else{
    $("pstat").innerHTML=`<span class="spin"></span>Downloaded ${d.done} of ${d.total}`;
    setTimeout(()=>poll(job),700);
  }

  $("dlist").innerHTML=Object.values(d.items).map(i=>{
    const st=i.state;
    const cls=st==="done"||st==="skipped"?"done":st==="error"?"error":st==="converting"?"conv":"";
    const sub=st==="downloading"
      ?`${i.percent}%${i.speed?" · "+i.speed:""}`
      :LABEL[st]||"";
    return `<div class="dl-item ${cls}">
      <div class="dl-top">
        <span class="icon">${ICONS[st]||"·"}</span>
        <span class="dl-name">${esc(i.title)}</span>
        <span class="dl-state">${sub}</span>
      </div>
      <div class="dl-bar"><i style="width:${i.percent}%"></i></div>
      ${i.error&&st==="error"?`<div class="dl-err">${esc(i.error)}</div>`:""}
    </div>`;
  }).join("");
}

function openFolder(){
  fetch("/api/open_folder",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:OUTDIR})});
}
$("url").addEventListener("keydown",e=>{if(e.key==="Enter")loadInfo()});
</script>
</div></body></html>"""


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    print("✅ ffmpeg found" if FFMPEG_OK
          else "⚠️  ffmpeg NOT found — conversion unavailable!\n   brew install ffmpeg")
    print(f"📁 Files → {SAVE_DIR}")
    url = f"http://127.0.0.1:{PORT}"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"🎬 Bulk Playlist Downloader → {url}  (Ctrl+C to quit)")
    app.run(port=PORT, debug=False)
