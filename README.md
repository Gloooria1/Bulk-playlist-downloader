# Bulk Playlist Downloader

Download entire **playlists**, **channels**, or **single videos** in one go — as
video (MP4) or audio (MP3/M4A/FLAC/…). A small local web app: run it and it opens
in your browser. Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp), so it works
with **YouTube, Vimeo, SoundCloud, Twitch, Dailymotion** and ~1800 other sites.

Every download is automatically saved in a universal format (**H.264 + AAC MP4**),
so it plays everywhere — QuickTime, Telegram, iPhone, and video editors. No more
"unsupported file" errors.

<img width="2160" height="2160" alt="screenshot" src="https://github.com/user-attachments/assets/5440e720-cbed-4cb3-b30c-bd299fbed5a2" />



## Features

- Paste a playlist / channel / video link → see every video, pick what you want
- Bulk download with a live per-video progress bar
- Video or audio-only, quality up to 4K, choice of audio format & bitrate
- Parallel downloads (1–3 at a time)
- Optional subtitles, metadata tags, and embedded thumbnails
- "Don't re-download" mode (keeps an archive of what you already grabbed)
- Use your browser cookies to fetch private / sign-in-required videos
- Search and filter within a large playlist before downloading

## Requirements

- **Python 3.9+**
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** and **Flask** (see install below)
- **[ffmpeg](https://ffmpeg.org/)** — needed for merging, MP3 conversion, and quality above 720p

## Install

```bash
# 1. Python dependencies
pip3 install -r requirements.txt

# 2. ffmpeg
#   macOS:    brew install ffmpeg
#   Ubuntu:   sudo apt install ffmpeg
#   Windows:  winget install Gyan.FFmpeg
```

## Run

```bash
python3 bulk_playlist_downloader.py
```

It starts a local server and opens **http://127.0.0.1:8090** in your browser.

> On macOS you can also double-click **`Launch Bulk Playlist Downloader.command`** in Finder.

## Where files go

Downloads are saved to **`~/Downloads/Playlists/<playlist name>/`** — each playlist
gets its own subfolder. (On every machine `~` is the current user's home folder, so
this path is correct for whoever runs the app.)

## Note

Only download content you have the right to (your own uploads, Creative Commons,
or with permission). Respect each site's Terms of Service and copyright law.

## License

MIT
