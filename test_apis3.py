#!/usr/bin/env python3
"""Test Kugou mobile URL quality and QQ Music smartbox+vkey flow."""
import sys, json, urllib.parse, requests, random
import urllib3
urllib3.disable_warnings()

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

test_songs = [
    {"title": "七里香", "artist": "周杰伦"},
    {"title": "人间烟火", "artist": "程响"},
    {"title": "一路生花", "artist": "温奕心"},
    {"title": "我想给你", "artist": "X玖少年团"},
]

print("=" * 80)
print("Kugou Mobile + QQ Music Smartbox Test")
print("=" * 80)

for song in test_songs:
    title = song["title"]
    artist = song["artist"]
    print(f"\n{'='*60}")
    print(f"  {title} - {artist}")
    print(f"{'='*60}")
    
    query = urllib.parse.quote(f"{title} {artist}")
    
    # ── Kugou: mobile API ──
    print("\n  [KUGOU Mobile]")
    try:
        # Search
        search_url = f"https://songsearch.kugou.com/song_search_v2?keyword={query}&page=1&pagesize=5"
        r = requests.get(search_url, headers={"User-Agent": UA, "Referer": "https://www.kugou.com/"}, timeout=15, verify=False)
        songs = (r.json().get("data") or {}).get("lists") or []
        if not songs:
            query2 = urllib.parse.quote(title)
            search_url2 = f"https://songsearch.kugou.com/song_search_v2?keyword={query2}&page=1&pagesize=5"
            r = requests.get(search_url2, headers={"User-Agent": UA, "Referer": "https://www.kugou.com/"}, timeout=15, verify=False)
            songs = (r.json().get("data") or {}).get("lists") or []
        
        if songs:
            s = songs[0]
            file_hash = s.get("FileHash") or s.get("filehash") or ""
            print(f"    Hash: {file_hash}")
            
            # Mobile API
            murl = f"https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash={file_hash}"
            r2 = requests.get(murl, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 10_3_1 like Mac OS X)"}, timeout=15, verify=False)
            if r2.status_code == 200:
                mdata = r2.json()
                play_url = mdata.get("url", "")
                ext = mdata.get("extName", "")
                fileSize = mdata.get("fileSize", 0)
                bitRate = mdata.get("bitRate", 0)
                print(f"    extName: {ext}, bitRate: {bitRate}, fileSize: {fileSize}")
                print(f"    url: {play_url[:80]}..." if play_url else "    url: EMPTY")
                
                if play_url:
                    # Check the actual content type
                    r3 = requests.head(play_url, timeout=10, allow_redirects=True, verify=False)
                    ct = r3.headers.get("Content-Type", "")
                    cl = r3.headers.get("Content-Length", "0")
                    print(f"    Content-Type: {ct}, Content-Length: {cl}")
                    
                    # Also try SQ hash
                    sq_hash = s.get("SQHash") or s.get("sqhash") or ""
                    hq_hash = s.get("HQHash") or s.get("hqhash") or ""
                    print(f"    SQHash: {sq_hash[:12] if sq_hash else 'NONE'}, HQHash: {hq_hash[:12] if hq_hash else 'NONE'}")
                    
                    # If SQ hash exists, try it
                    if sq_hash:
                        murl_sq = f"https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash={sq_hash}"
                        r4 = requests.get(murl_sq, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 10_3_1 like Mac OS X)"}, timeout=15, verify=False)
                        if r4.status_code == 200:
                            sqdata = r4.json()
                            sq_url = sqdata.get("url", "")
                            sq_ext = sqdata.get("extName", "")
                            sq_br = sqdata.get("bitRate", 0)
                            print(f"    [SQ] extName: {sq_ext}, bitRate: {sq_br}, url: {'YES' if sq_url else 'NO'}")
                            if sq_url:
                                r5 = requests.head(sq_url, timeout=10, allow_redirects=True, verify=False)
                                print(f"    [SQ] Content-Type: {r5.headers.get('Content-Type','')}, CL: {r5.headers.get('Content-Length','0')}")
        else:
            print("    No search results")
    except Exception as e:
        print(f"    Error: {e}")
    
    # ── QQ Music: smartbox + vkey ──
    print("\n  [QQ Music Smartbox]")
    try:
        query_enc = urllib.parse.quote(f"{title} {artist}")
        smartbox_url = f"https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg?key={query_enc}&format=json"
        r = requests.get(smartbox_url, headers={"User-Agent": UA, "Referer": "https://y.qq.com/"}, timeout=15)
        items = []
        if r.status_code == 200:
            j = r.json()
            items = j.get("data", {}).get("song", {}).get("itemlist", [])
        
        if not items:
            # Try just title
            query_enc2 = urllib.parse.quote(title)
            smartbox_url2 = f"https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg?key={query_enc2}&format=json"
            r = requests.get(smartbox_url2, headers={"User-Agent": UA, "Referer": "https://y.qq.com/"}, timeout=15)
            if r.status_code == 200:
                items = r.json().get("data", {}).get("song", {}).get("itemlist", [])
        
        if items:
            item = items[0]
            mid = item.get("mid", "")
            song_id = item.get("id", "")
            print(f"    mid: {mid}, id: {song_id}, name: {item.get('name','')}")
            
            # Try to get vkey for this mid
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
            vkey_url = "https://u.y.qq.com/cgi-bin/musicu.fcg?data=" + urllib.parse.quote(vkey_payload)
            r2 = requests.get(vkey_url, headers={"User-Agent": UA, "Referer": "https://y.qq.com/"}, timeout=15)
            vdata = r2.json()
            midurlinfo = vdata.get("req_0", {}).get("data", {}).get("midurlinfo") or []
            sip = vdata.get("req_0", {}).get("data", {}).get("sip") or []
            
            if midurlinfo and sip:
                info = midurlinfo[0]
                vkey = info.get("vkey", "")
                filename = info.get("filename", "")
                purl = info.get("purl", "")
                print(f"    filename: {filename}, purl: {purl[:40] if purl else 'EMPTY'}, vkey: {'YES' if vkey else 'NO'}")
                
                if vkey:
                    # Try M500 (MP3 320) first to see if download works
                    for prefix, fmt_name in [("M500", "MP3-320"), ("F000", "FLAC"), ("C400", "MP3-128")]:
                        test_filename = prefix + filename[4:] if filename.startswith(("M500", "C400", "F000")) else f"{prefix}{mid}.{'flac' if prefix == 'F000' else 'mp3'}"
                        dl_url = f"{sip[0]}{test_filename}?guid={guid}&vkey={vkey}&fromtag=46&uin=0"
                        r3 = requests.head(dl_url, timeout=10, allow_redirects=True, verify=False)
                        ct = r3.headers.get("Content-Type", "")
                        cl = r3.headers.get("Content-Length", "0")
                        sc = r3.status_code
                        print(f"    [{fmt_name}] {test_filename[:20]}... status={sc}, CT={ct}, CL={cl}")
            else:
                print(f"    No vkey data. Response: {json.dumps(vdata.get('req_0',{}).get('data',{}))[:200]}")
        else:
            print("    No smartbox results")
    except Exception as e:
        print(f"    Error: {e}")

print("\n" + "=" * 80)
