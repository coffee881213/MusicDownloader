#!/usr/bin/env python3
"""Test alternative Chinese music API endpoints for FLAC."""
import sys, json, urllib.parse, requests, hashlib, random
import urllib3
urllib3.disable_warnings()

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

test_song = {"title": "七里香", "artist": "周杰伦"}

print("=" * 80)
print(f"Testing: {test_song['title']} - {test_song['artist']}")
print("=" * 80)

# ── 1. NetEase: Try multiple URL endpoints ──
print("\n--- NetEase ---")
headers = {"User-Agent": UA, "Referer": "https://music.163.com/"}
try:
    r = requests.post("https://music.163.com/api/cloudsearch/pc",
        data={"s": f"{test_song['title']} {test_song['artist']}", "type": "1", "offset": "0", "total": "true", "limit": "5"},
        headers=headers, timeout=15)
    songs = (r.json().get("result") or {}).get("songs") or []
    print(f"  Search: {len(songs)} results")
    if songs:
        sid = songs[0]["id"]
        print(f"  Song ID: {sid}")
        
        # Test 1a: song/url/v1 with level=lossless
        for level in ["lossless", "hires", "exhigh", "standard"]:
            r2 = requests.post(f"https://music.163.com/api/song/url/v1?id={sid}&level={level}",
                headers=headers, timeout=15)
            data = r2.json().get("data") or []
            if data:
                d = data[0]
                print(f"  [v1 level={level}] url={'YES' if d.get('url') else 'NO'}, br={d.get('br',0)}, type={d.get('type','')}, size={d.get('size',0)}")
            else:
                print(f"  [v1 level={level}] No data")
        
        # Test 1b: enhance/player/url (older API)
        r3 = requests.post(f"https://music.163.com/api/song/enhance/player/url?id={sid}&br=999000",
            headers=headers, timeout=15)
        data3 = r3.json().get("data") or []
        if data3:
            d = data3[0]
            print(f"  [enhance] url={'YES' if d.get('url') else 'NO'}, br={d.get('br',0)}, type={d.get('type','')}")
        else:
            print(f"  [enhance] No data")
        
        # Test 1c: outer URL redirect
        outer = f"https://music.163.com/song/media/outer/url?id={sid}"
        r4 = requests.get(outer, headers=headers, timeout=15, allow_redirects=False)
        print(f"  [outer] status={r4.status_code}, Location={r4.headers.get('Location','')[:80]}")
        if r4.status_code == 302:
            loc = r4.headers.get("Location", "")
            if loc:
                r5 = requests.head(loc, headers=headers, timeout=10, allow_redirects=True)
                print(f"  [outer-redirect] CT={r5.headers.get('Content-Type','')}, CL={r5.headers.get('Content-Length','')}")
except Exception as e:
    print(f"  Error: {e}")

# ── 2. QQ Music: Try multiple search endpoints ──
print("\n--- QQ Music ---")
headers_qq = {"User-Agent": UA, "Referer": "https://y.qq.com/"}
try:
    # Test 2a: musicu.fcg with different module
    payload = json.dumps({
        "req_0": {
            "module": "music.search.SearchCgiService",
            "method": "DoSearchForQQMusicDesktop",
            "param": {"query": f"{test_song['title']} {test_song['artist']}", "search_type": 0, "num": 5, "page_num": 1}
        },
        "comm": {"uin": "0", "format": "json", "ct": 24, "cv": 0}
    })
    url = "https://u.y.qq.com/cgi-bin/musicu.fcg?data=" + urllib.parse.quote(payload)
    r = requests.get(url, headers=headers_qq, timeout=15)
    j = r.json()
    req0 = j.get("req_0", {})
    code = req0.get("code")
    data = req0.get("data", {})
    body = data.get("body", {}) if data else {}
    song = body.get("song", {}) if body else {}
    qsongs = song.get("list", []) if song else []
    print(f"  [musicu.fcg] code={code}, qsongs={len(qsongs)}")
    if qsongs:
        s = qsongs[0]
        mid = s.get("mid") or s.get("songmid")
        f = s.get("file", {})
        print(f"  First: mid={mid}, size_flac={f.get('size_flac',0)}, size_320={f.get('size_320',0)}")
    
    # Test 2b: client_search_cp
    query_enc = urllib.parse.quote(f"{test_song['title']} {test_song['artist']}")
    url2 = f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?format=json&inCharset=utf8&outCharset=utf-8&key={query_enc}&num=5&t=0&lossless=1"
    r2 = requests.get(url2, headers=headers_qq, timeout=15)
    print(f"  [client_search_cp] status={r2.status_code}, len={len(r2.text)}")
    if r2.status_code == 200:
        try:
            j2 = r2.json()
            qsongs2 = ((j2.get("data") or {}).get("song") or {}).get("list") or []
            print(f"  [client_search_cp] qsongs={len(qsongs2)}")
            if qsongs2:
                s = qsongs2[0]
                f = s.get("file") or s.get("File") or {}
                print(f"  First: mid={s.get('mid') or s.get('songmid')}, size_flac={f.get('size_flac',0)}")
        except Exception as e:
            print(f"  [client_search_cp] JSON parse error: {e}, text[:100]={r2.text[:100]}")
    
    # Test 2c: smartbox search
    url3 = f"https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg?key={query_enc}&format=json"
    r3 = requests.get(url3, headers=headers_qq, timeout=15)
    print(f"  [smartbox] status={r3.status_code}")
    if r3.status_code == 200:
        try:
            j3 = r3.json()
            data3 = j3.get("data", {})
            song3 = data3.get("song", {})
            itemlist = song3.get("itemlist", [])
            print(f"  [smartbox] items={len(itemlist)}")
            if itemlist:
                print(f"  First: {itemlist[0]}")
        except:
            pass
except Exception as e:
    print(f"  Error: {e}")

# ── 3. Kugou: Try mobile API ──
print("\n--- Kugou ---")
headers_kg = {"User-Agent": UA, "Referer": "https://www.kugou.com/"}
try:
    query = urllib.parse.quote(f"{test_song['title']} {test_song['artist']}")
    url = f"https://songsearch.kugou.com/song_search_v2?keyword={query}&page=1&pagesize=5"
    r = requests.get(url, headers=headers_kg, timeout=15, verify=False)
    songs = (r.json().get("data") or {}).get("lists") or []
    print(f"  Search: {len(songs)} results")
    if songs:
        s = songs[0]
        file_hash = s.get("FileHash") or s.get("filehash") or ""
        album_id = s.get("AlbumID") or s.get("album_id") or ""
        print(f"  Hash={file_hash}, AlbumID={album_id}")
        
        # Test 3a: wwwapi play/getdata
        dfid = ''.join(random.choice('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz') for _ in range(24))
        mid_val = hashlib.md5(dfid.encode()).hexdigest().upper()
        play_url = f"https://wwwapi.kugou.com/yy/index.php?r=play/getdata&hash={file_hash}&dfid={dfid}&mid={mid_val}&platid=4&album_id={album_id}"
        r2 = requests.get(play_url, headers=headers_kg, timeout=15, verify=False)
        pdata = r2.json().get("data") or {}
        print(f"  [wwwapi] play_url={'YES' if pdata.get('play_url') else 'EMPTY'}, extras={pdata.get('extras',{})}")
        
        # Test 3b: mobile API
        murl = f"https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash={file_hash}"
        r3 = requests.get(murl, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 10_3_1 like Mac OS X)"}, timeout=15, verify=False)
        print(f"  [mobile] status={r3.status_code}")
        if r3.status_code == 200:
            try:
                mdata = r3.json()
                print(f"  [mobile] url={'YES' if mdata.get('url') else 'NO'}, status={mdata.get('status')}, img='{mdata.get('img','')[:30]}'")
                if mdata.get("url"):
                    print(f"  [mobile] url={mdata['url'][:80]}")
            except:
                print(f"  [mobile] Not JSON: {r3.text[:100]}")
        
        # Test 3c: try with different hash types (SQ, HQ)
        # Search for FLAC quality hash
        url2 = f"https://songsearch.kugou.com/song_search_v2?keyword={query}&page=1&pagesize=5&showtype=10&version=10710"
        r4 = requests.get(url2, headers=headers_kg, timeout=15, verify=False)
        songs4 = (r4.json().get("data") or {}).get("lists") or []
        if songs4:
            s4 = songs4[0]
            # Check for SQHash or HQHash
            print(f"  [search-v2] FileHash={s4.get('FileHash','')[:12]}, Atrac={s4.get('Atrac',0)}, Hq={s4.get('Hq',0)}, Sq={s4.get('Sq',0)}, Master={s4.get('Master',0)}")
except Exception as e:
    print(f"  Error: {e}")

# ── 4. Migu: Try mobile web API ──
print("\n--- Migu ---")
headers_mg = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
    "Referer": "https://music.migu.cn/",
}
try:
    query = urllib.parse.quote(f"{test_song['title']} {test_song['artist']}")
    
    # Test 4a: pd.musicapp.migu.cn
    url1 = f"https://pd.musicapp.migu.cn/MIGUM3.0/v1.0/content/search_all.do?ua=Android_migu&version=5.0.1&text={query}&pageNo=1&pageSize=5&searchSwitch=%7B%22song%22%3A1%7D"
    r = requests.get(url1, headers=headers_mg, timeout=15, verify=False)
    print(f"  [pd.musicapp] status={r.status_code}")
    if r.status_code == 200:
        try:
            mdata = r.json()
            musics = (mdata.get("songResultData") or {}).get("result") or []
            if not musics:
                musics = mdata.get("songList") or []
            print(f"  [pd.musicapp] musics={len(musics)}")
            if musics:
                m = musics[0]
                cid = m.get("copyrightId") or ""
                song_id = m.get("id") or ""
                print(f"  [pd.musicapp] cid={cid}, songId={song_id}")
                # Print all rate formats
                rates = m.get("newRateFormats") or []
                if isinstance(rates, list):
                    for rt in rates:
                        print(f"    rate: {rt.get('formatType','')} -> url={'YES' if rt.get('url') else 'NO'}")
                # Print full song data keys
                print(f"    keys: {list(m.keys())[:15]}")
        except:
            print(f"  [pd.musicapp] Not JSON: {r.text[:100]}")
    
    # Test 4b: m.music.migu.cn (mobile web)
    url2 = f"https://m.music.migu.cn/migu/remoting/scr_search_tag?keyword={query}&type=2&rows=5&page=1"
    r2 = requests.get(url2, headers=headers_mg, timeout=15, verify=False)
    print(f"  [m.music.migu] status={r2.status_code}, len={len(r2.text)}")
    if r2.status_code == 200:
        try:
            j2 = r2.json()
            musics2 = j2.get("musics") or []
            print(f"  [m.music.migu] musics={len(musics2)}")
            if musics2:
                m = musics2[0]
                print(f"  [m.music.migu] keys: {list(m.keys())[:15]}")
                # Check for mp3 or flac URL
                for k in ["mp3", "flac", "hqloss", "sqloss", "url"]:
                    if m.get(k):
                        print(f"    {k}={str(m[k])[:80]}")
        except:
            print(f"  [m.music.migu] Not JSON: {r2.text[:100]}")
    
    # Test 4c: music.migu.cn/v3/api
    url3 = f"https://music.migu.cn/v3/api/music/searchAll?text={query}&pageNo=1&pageSize=5"
    r3 = requests.get(url3, headers={"User-Agent": UA, "Referer": "https://music.migu.cn/"}, timeout=15, verify=False)
    print(f"  [music.migu.cn/v3] status={r3.status_code}")
    if r3.status_code == 200:
        try:
            j3 = r3.json()
            songList = j3.get("songResultData", {}).get("result", []) or []
            print(f"  [music.migu.cn/v3] songs={len(songList)}")
            if songList:
                m = songList[0]
                cid = m.get("copyrightId", "")
                print(f"  cid={cid}, keys={list(m.keys())[:15]}")
                # Try audioPlayer/songs API
                if cid:
                    aurl = f"https://music.migu.cn/v3/api/music/audioPlayer/songs?copyrightId={cid}"
                    r4 = requests.get(aurl, headers={"User-Agent": UA, "Referer": "https://music.migu.cn/"}, timeout=15, verify=False)
                    print(f"  [audioPlayer/songs] status={r4.status_code}")
                    if r4.status_code == 200:
                        try:
                            adata = r4.json()
                            items = adata if isinstance(adata, list) else [adata]
                            for item in items:
                                if isinstance(item, dict):
                                    print(f"    keys: {list(item.keys())[:15]}")
                                    for k in ["flacUrl", "playUrl", "url", "newRateFormats"]:
                                        v = item.get(k)
                                        if v:
                                            if isinstance(v, dict):
                                                print(f"    {k}: {list(v.keys())[:5]}")
                                            elif isinstance(v, list):
                                                for vi in v[:3]:
                                                    if isinstance(vi, dict):
                                                        print(f"    {k}[{vi.get('formatType','')}]: url={'YES' if vi.get('url') else 'NO'}")
                                            else:
                                                print(f"    {k}: {str(v)[:60]}")
                        except:
                            print(f"  [audioPlayer/songs] Not JSON: {r4.text[:100]}")
        except:
            print(f"  [music.migu.cn/v3] Not JSON: {r3.text[:100]}")
except Exception as e:
    print(f"  Error: {e}")

print("\n" + "=" * 80)
