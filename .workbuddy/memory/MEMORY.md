# Hi-Res Music Downloader - Project Memory

## Project Overview
- 10-platform parallel music downloader for Windows 11 CMD
- Target: 24-bit/192kHz+ FLAC, OST priority (any format), FLAC fallback (non-OST), no non-OST lossy
- Download directory: D:\MyMusic (auto-detected: D:\MyMusic if D: drive exists, C:\MyMusic if D: not found)
- Interactive selection: Arrow-key navigation using _arrow_select_or_input() (msvcrt on Windows)
  - Up/Down arrows move focus between "use default" and "input custom"
  - When focused on default: green+bold text, green hint line
  - When unfocused default: dim text, hint hidden
  - Layout: hints outside box (2 lines, cyan color, no dim), input box inside is EMPTY with 3-line height
  - When focused on input box: border yellow+bold, █ cursor appears, accepts typing
  - When unfocused input box: border dim cyan, box content empty
  - Input box right-side │ alignment uses visible char length (not ANSI-included length)
  - Backspace deletes, ESC clears input, empty Enter ignored
  - Falls back to numbered menu in non-TTY environments
- Original project path: C:\Users\Administrator\codebuddy\20260705115345

## 10 Download Platforms (priority order)
1. Sockseek (Soulseek P2P) - user: zpf10284140 / pass: zpf123, - min-bitrate: 3000
2. Internet Archive - Public domain FLAC
3. Free Music Archive - Free FLAC
4. Jamendo - Free FLAC
5. YouTube via yt-dlp - Best audio -> FLAC
6. Bandcamp via yt-dlp - Free releases FLAC
7. NetEase Cloud Music (网易云音乐) - cloudsearch POST + outer URL
8. QQ Music (QQ音乐) - smartbox search + vkey
9. Kugou Music (酷狗音乐) - songsearch + m.kugou.com mobile API
10. Migu Music (咪咕音乐) - MIGUM3.0 search API

## Key Technical Conventions
- Download policy: OST files accepted in ANY format (FLAC/WAV/APE/MP3/etc.); non-OST files only if FLAC. Non-OST non-FLAC rejected.
- OST priority: +30 score bonus for soundtrack/原声带 files (applied to ALL formats including MP3)
- Filter: _keep_if_better() checks `is_ost OR is_flac` (not quality threshold)
- MP3 cleanup: _cleanup_mp3_in_dir() deletes non-OST MP3s only; keeps OST MP3s
- Sockseek config: format includes ALL audio formats; post-download filter rejects non-OST non-FLAC
- Chinese platforms: Download available audio → ffmpeg convert to FLAC
- Sockseek: Serialized (1 slot), _sockseek_offline flag for fast skip
- Sockseek: NO --proxy option, uses raw TCP to server.slsknet.org:2242 (direct connection works from CN)
- Proxy: Auto-detected via _setup_proxy() → _auto_detect_proxy() (env vars → Windows registry → urllib.getproxies)
  - Sets ALL env vars: http_proxy/https_proxy/HTTP_PROXY/HTTPS_PROXY/all_proxy/ALL_PROXY + NO_PROXY for localhost
  - Installs proxy-aware urllib opener; resets requests Session
  - If no proxy detected: clears ALL proxy env vars + installs direct opener
  - Effective for: requests, urllib, yt-dlp, aria2c, sockseek/.NET, all subprocesses
- ffmpeg available at: C:\Program Files\ffmpeg\bin\ffmpeg.exe
- Python venv: C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe
- Default playlist: https://c6.y.qq.com/base/fcgi-bin/u?__=RI5L3W4QHae1
- Short URL resolution: Uses GET with stream=True (HEAD returns 500 for c6.y.qq.com)

## Quality Scoring
- QUALITY_HIRESFLAC = 100 (24-bit >= 192kHz)
- QUALITY_FLAC96 = 90, FLAC48 = 80, FLAC16 = 70
- OST_BONUS = 30 (added to base score for OST files — ensures OST always ranks above non-OST FLAC)
- WAV = 65, LOSSLESS = 60, MP3_320 = 40, MP3_HIGH = 30, AAC_HIGH = 25, LOSSY = 10
