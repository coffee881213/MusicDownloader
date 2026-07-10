#!/usr/bin/env python3
"""Test Chinese music platform APIs for FLAC download."""
import requests, json, urllib.parse, warnings, hashlib, random
warnings.filterwarnings('ignore')

query = '人间烟火'
q = urllib.parse.quote(query)
headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# === NetEase: try outer URL without .flac ===
print('=== NetEase outer URL (no ext) ===')
r = requests.post('https://music.163.com/api/cloudsearch/pc',
    data={'s': query, 'type': '1', 'offset': '0', 'total': 'true', 'limit': '5'},
    headers={**headers, 'Referer': 'https://music.163.com/'}, timeout=15)
songs = (r.json().get('result') or {}).get('songs') or []
if songs:
    song_id = songs[1].get('id') if len(songs) > 1 else songs[0].get('id')
    name = songs[1].get('name') if len(songs) > 1 else songs[0].get('name')
    print(f'Song ID: {song_id}, name: {name}')
    outer = f'https://music.163.com/song/media/outer/url?id={song_id}'
    r2 = requests.get(outer, headers={**headers, 'Referer': 'https://music.163.com/'},
                      timeout=15, allow_redirects=False)
    print(f'  Redirect status: {r2.status_code}')
    loc = r2.headers.get("Location", "")
    print(f'  Location: {loc[:120]}')
    if r2.status_code == 302 and loc and 'http' in loc:
        r3 = requests.head(loc, headers=headers, timeout=15, allow_redirects=True)
        print(f'  Final: status={r3.status_code} type={r3.headers.get("Content-Type","")} size={r3.headers.get("Content-Length","")}')

# === QQ Music: try ct=24, cv=0 ===
print('\n=== QQ Music ct=24 ===')
payload = json.dumps({
    "req_0": {
        "module": "music.search.SearchCgiService",
        "method": "DoSearchForQQMusicDesktop",
        "param": {"query": query, "search_type": 0, "num": 5, "page_num": 1}
    },
    "comm": {"uin": "0", "format": "json", "ct": 24, "cv": 0}
})
r3 = requests.get('https://u.y.qq.com/cgi-bin/musicu.fcg?data=' + urllib.parse.quote(payload),
    headers={**headers, 'Referer': 'https://y.qq.com/'}, timeout=15)
j = r3.json()
req0 = j.get('req_0', {})
print(f'code: {req0.get("code")}')
data = req0.get('data', {})
print(f'data keys: {list(data.keys())}')
body = data.get('body', {})
print(f'body keys: {list(body.keys())}')
song = body.get('song', {})
print(f'song keys: {list(song.keys())}')
qsongs = song.get('list', [])
print(f'Songs: {len(qsongs)}')
for s in qsongs[:3]:
    mid = s.get('mid')
    name = s.get('name')
    singer = s.get('singer', [{}])[0].get('name', '')
    f = s.get('file', {})
    print(f'  mid={mid} name={name} singer={singer} size_flac={f.get("size_flac", 0)}')

# === Kugou: with dfid ===
print('\n=== Kugou with dfid ===')
dfid_chars = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
dfid = ''.join(random.choice(dfid_chars) for _ in range(24))
mid_val = hashlib.md5(dfid.encode()).hexdigest().upper()

r5 = requests.get(f'https://songsearch.kugou.com/song_search_v2?keyword={q}&page=1&pagesize=5',
    headers={**headers, 'Referer': 'https://www.kugou.com/'}, timeout=15, verify=False)
ksongs = (r5.json().get('data') or {}).get('lists') or []
if ksongs:
    for s in ksongs:
        if '程响' in (s.get('SingerName', '') or ''):
            file_hash = s.get('FileHash')
            album_id = s.get('AlbumID', '')
            print(f'Found: {s.get("SongName")} by {s.get("SingerName")}')
            print(f'  hash={file_hash} album={album_id}')
            play_url = f'https://wwwapi.kugou.com/yy/index.php?r=play/getdata&hash={file_hash}&dfid={dfid}&mid={mid_val}&platid=4&album_id={album_id}'
            r6 = requests.get(play_url,
                headers={**headers, 'Referer': f'https://www.kugou.com/song/#hash={file_hash}&album_id={album_id}'},
                timeout=15, verify=False)
            pdata = r6.json().get('data') or {}
            print(f'  play_url: {pdata.get("play_url", "")[:80]}')
            print(f'  play_backup_url: {pdata.get("play_backup_url", "")[:80]}')
            print(f'  quality_type: {pdata.get("quality_type")}')
            extras = pdata.get('extras') or {}
            print(f'  extras keys: {list(extras.keys())}')
            if extras.get('flac_hash'):
                print(f'  flac_hash: {extras.get("flac_hash")}')
            break
