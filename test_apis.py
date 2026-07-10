#!/usr/bin/env python3
"""Quick test of Chinese music platform APIs for FLAC availability."""
import sys, os, json, urllib.parse, requests, hashlib, random, string

# Suppress SSL warnings
import urllib3
urllib3.disable_warnings()

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

test_songs = [
    {"title": "人间烟火", "artist": "程响"},
    {"title": "一路生花", "artist": "温奕心"},
    {"title": "七里香", "artist": "周杰伦"},
    {"title": "Shell Shocked", "artist": "Wiz Khalifa"},
]

def test_netease(title, artist):
    """Test NetEase Cloud Music API."""
    headers = {"User-Agent": UA, "Referer": "https://music.163.com/"}
    try:
        r = requests.post(
            "https://music.163.com/api/cloudsearch/pc",
            data={"s": f"{title} {artist}", "type": "1", "offset": "0",
                  "total": "true", "limit": "5"},
            headers=headers, timeout=15
        )
        songs = (r.json().get("result") or {}).get("songs") or []
        if not songs:
            return f"  [NETEASE] No search results"
        sid = songs[0]["id"]
        # Try song/url/v1 with lossless level
        r2 = requests.post(
            f"https://music.163.com/api/song/url/v1?id={sid}&level=lossless",
            headers=headers, timeout=15
        )
        data = r2.json().get("data") or []
        if data:
            d = data[0]
            url = d.get("url")
            br = d.get("br", 0)
            ext = d.get("type", "")
            size = d.get("size", 0)
            if url:
                return f"  [NETEASE] FOUND: br={br}, type={ext}, size={size/1024/1024:.1f}MB, url={url[:60]}..."
            else:
                return f"  [NETEASE] Found song but no URL (br={br}, type={ext})"
        return f"  [NETEASE] No URL data"
    except Exception as e:
        return f"  [NETEASE] Error: {e}"


def test_qqmusic(title, artist):
    """Test QQ Music API."""
    headers = {"User-Agent": UA, "Referer": "https://y.qq.com/"}
    try:
        payload = json.dumps({
            "req_0": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {"query": f"{title} {artist}", "search_type": 0, "num": 5, "page_num": 1}
            },
            "comm": {"uin": "0", "format": "json", "ct": 24, "cv": 0}
        })
        url = "https://u.y.qq.com/cgi-bin/musicu.fcg?data=" + urllib.parse.quote(payload)
        r = requests.get(url, headers=headers, timeout=15)
        j = r.json()
        qsongs = j.get("req_0", {}).get("data", {}).get("body", {}).get("song", {}).get("list", [])
        if not qsongs:
            return f"  [QQMUSIC] No search results"
        s = qsongs[0]
        mid = s.get("mid") or s.get("songmid")
        f = s.get("file", {})
        size_flac = f.get("size_flac", 0) if f else 0
        size_320 = f.get("size_320", 0) if f else 0
        size_128 = f.get("size_128", 0) if f else 0
        info = f"mid={mid}, size_flac={size_flac}, size_320={size_320}"
        if size_flac and size_flac > 1000:
            # Try to get vkey for FLAC
            guid = str(random.randint(1000000000, 9999999999))
            vkey_payload = json.dumps({
                "req_0": {
                    "module": "vkey.GetVkeyServer",
                    "method": "CgiGetVkey",
                    "param": {
                        "guid": guid, "songmid": [mid], "songtype": [0],
                        "uin": "0", "loginflag": 1, "platform": "20"
                    }
                },
                "comm": {"uin": 0, "format": "json", "ct": 24, "cv": 0}
            })
            vurl = "https://u.y.qq.com/cgi-bin/musicu.fcg?data=" + urllib.parse.quote(vkey_payload)
            r2 = requests.get(vurl, headers=headers, timeout=15)
            vdata = r2.json()
            midurlinfo = vdata.get("req_0", {}).get("data", {}).get("midurlinfo") or []
            sip = vdata.get("req_0", {}).get("data", {}).get("sip") or []
            if midurlinfo and sip:
                info_obj = midurlinfo[0]
                vkey = info_obj.get("vkey", "")
                filename = info_obj.get("filename", "")
                if vkey:
                    # Try FLAC filename
                    if filename.startswith("M500"):
                        filename = "F000" + filename[4:]
                    dl_url = f"{sip[0]}{filename}?guid={guid}&vkey={vkey}&fromtag=46"
                    # Check if URL returns audio
                    r3 = requests.head(dl_url, headers=headers, timeout=10, allow_redirects=True)
                    ct = r3.headers.get("Content-Type", "")
                    cl = r3.headers.get("Content-Length", "0")
                    return f"  [QQMUSIC] FLAC available! {info} | CT={ct}, CL={cl}, url={dl_url[:60]}..."
                return f"  [QQMUSIC] FLAC size={size_flac} but no vkey"
            return f"  [QQMUSIC] FLAC size={size_flac} but no midurlinfo/sip"
        return f"  [QQMUSIC] {info} (no FLAC)"
    except Exception as e:
        return f"  [QQMUSIC] Error: {e}"


def test_kugou(title, artist):
    """Test Kugou Music API."""
    headers = {"User-Agent": UA, "Referer": "https://www.kugou.com/"}
    try:
        query = urllib.parse.quote(f"{title} {artist}")
        url = f"https://songsearch.kugou.com/song_search_v2?keyword={query}&page=1&pagesize=5"
        r = requests.get(url, headers=headers, timeout=15, verify=False)
        songs = (r.json().get("data") or {}).get("lists") or []
        if not songs:
            return f"  [KUGOU] No search results"
        s = songs[0]
        file_hash = s.get("FileHash") or s.get("filehash") or ""
        album_id = s.get("AlbumID") or s.get("album_id") or ""
        # Generate dfid and mid
        dfid = ''.join(random.choice('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz') for _ in range(24))
        mid_val = hashlib.md5(dfid.encode()).hexdigest().upper()
        play_url = (
            f"https://wwwapi.kugou.com/yy/index.php?r=play/getdata"
            f"&hash={file_hash}&dfid={dfid}&mid={mid_val}"
            f"&platid=4&album_id={album_id}"
        )
        r2 = requests.get(play_url, headers=headers, timeout=15, verify=False)
        pdata = r2.json().get("data") or {}
        play_url_val = pdata.get("play_url") or ""
        extras = pdata.get("extras") or {}
        flac_hash = extras.get("flac_hash") or extras.get("sq_hash") or ""
        info = f"hash={file_hash[:12]}..., play_url={'YES' if play_url_val else 'EMPTY'}, flac_hash={'YES' if flac_hash else 'NO'}"
        if play_url_val and ".flac" in play_url_val.lower():
            return f"  [KUGOU] FLAC URL found! {info}"
        elif play_url_val:
            return f"  [KUGOU] Non-FLAC play_url: {info}, url_ext={play_url_val[-10:]}"
        elif flac_hash:
            # Try with flac_hash
            play_url2 = (
                f"https://wwwapi.kugou.com/yy/index.php?r=play/getdata"
                f"&hash={flac_hash}&dfid={dfid}&mid={mid_val}"
                f"&platid=4&album_id={album_id}"
            )
            r3 = requests.get(play_url2, headers=headers, timeout=15, verify=False)
            fdata = r3.json().get("data") or {}
            flac_url = fdata.get("play_url") or ""
            if flac_url:
                return f"  [KUGOU] FLAC via flac_hash! {info}, flac_url={'YES'}"
            return f"  [KUGOU] flac_hash found but no URL: {info}"
        return f"  [KUGOU] No play URL or flac_hash: {info}"
    except Exception as e:
        return f"  [KUGOU] Error: {e}"


def test_migu(title, artist):
    """Test Migu Music API."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
        "Referer": "https://music.migu.cn/",
        "channel": "0146921",
    }
    try:
        query = urllib.parse.quote(f"{title} {artist}")
        search_urls = [
            f"https://pd.musicapp.migu.cn/MIGUM3.0/v1.0/content/search_all.do?ua=Android_migu&version=5.0.1&text={query}&pageNo=1&pageSize=5&searchSwitch=%7B%22song%22%3A1%7D",
            f"https://jadeite.migu.cn/music_search/v2/search/searchAll?searchSwitch=%7B%22song%22%3A1%7D&text={query}&pageNo=1&pageSize=5",
        ]
        musics = []
        used_url = ""
        for surl in search_urls:
            try:
                r = requests.get(surl, headers=headers, timeout=15, verify=False)
                if r.status_code == 200:
                    try:
                        mdata = r.json()
                        musics = (mdata.get("songResultData") or {}).get("result") or []
                        if not musics:
                            musics = mdata.get("songList") or []
                        if musics:
                            used_url = surl[:40]
                            break
                    except:
                        pass
            except:
                continue
        if not musics:
            return f"  [MIGU] No search results"
        m = musics[0]
        cid = m.get("copyrightId") or m.get("copyrightId") or ""
        song_id = m.get("id") or m.get("songId") or ""
        # Try to get song details
        detail_urls = [
            f"https://music.migu.cn/v3/api/music/audioPlayer/songs?copyrightId={cid}",
            f"https://pd.musicapp.migu.cn/MIGUM3.0/v1.0/content/content/song.do?copyrightId={cid}&resourceType=2",
        ]
        for durl in detail_urls:
            try:
                r2 = requests.get(durl, headers=headers, timeout=15, verify=False)
                if r2.status_code != 200:
                    continue
                sdata = r2.json()
                items = sdata if isinstance(sdata, list) else [sdata]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    dl_url = item.get("flacUrl") or item.get("playUrl") or item.get("url") or ""
                    nrf = item.get("newRateFormats") or {}
                    if not dl_url and isinstance(nrf, dict):
                        dl_url = nrf.get("flac") or nrf.get("sq") or ""
                    elif not dl_url and isinstance(nrf, list):
                        for fmt in nrf:
                            if isinstance(fmt, dict) and fmt.get("formatType") in ("FLAC", "SQ", "ZQ"):
                                dl_url = fmt.get("url") or ""
                                if dl_url:
                                    break
                    if dl_url and "flac" in dl_url.lower():
                        return f"  [MIGU] FLAC URL found! cid={cid}, url={dl_url[:60]}..."
                    elif dl_url:
                        return f"  [MIGU] Non-FLAC URL: cid={cid}, url={dl_url[:60]}..."
                return f"  [MIGU] Found song cid={cid} but no download URL"
            except:
                continue
        return f"  [MIGU] Found song cid={cid} but detail API failed"
    except Exception as e:
        return f"  [MIGU] Error: {e}"


print("=" * 80)
print("Chinese Music Platform API Test")
print("=" * 80)

for song in test_songs:
    title = song["title"]
    artist = song["artist"]
    print(f"\n--- {title} - {artist} ---")
    print(test_netease(title, artist))
    print(test_qqmusic(title, artist))
    print(test_kugou(title, artist))
    print(test_migu(title, artist))

print("\n" + "=" * 80)
print("Test complete.")
