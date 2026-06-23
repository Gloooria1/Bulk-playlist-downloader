#!/bin/bash
# Double-click this file in Finder to launch Bulk Playlist Downloader.
# It starts a small local server and opens the app in your browser.
cd "$(dirname "$0")"
exec python3 bulk_playlist_downloader.py
