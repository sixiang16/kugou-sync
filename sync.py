#!/usr/bin/env python3
"""酷狗歌单自动同步下载服务 - 适用于飞牛NAS Docker"""
import os, re, json, time, requests, schedule
from pathlib import Path

# ----- 配置（全部从环境变量读取） -----
DOWNLOAD_DIR    = os.getenv('DOWNLOAD_DIR', '/music')
KUGOU_COOKIE    = os.getenv('KUGOU_COOKIE', '')
INTERVAL_MIN    = int(os.getenv('INTERVAL_MIN', '60'))
PLAYLIST_IDS    = os.getenv('PLAYLIST_IDS', '')
PLAYLIST_NAMES  = os.getenv('PLAYLIST_NAMES', '')

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})
if KUGOU_COOKIE:
    for item in KUGOU_COOKIE.split(';'):
        if '=' in item:
            k, v = item.strip().split('=', 1)
            session.cookies.set(k, v)

def safe_name(text):
    return re.sub(r'[\\/*?:"<>|]', '_', text)

def get_playlist_name(pid):
    """获取歌单名字（失败时回退为ID）"""
    try:
        r = session.get(f'https://www.kugou.com/yy/playlist/{pid}.html', timeout=10)
        r.encoding = 'utf-8'
        # 尝试从 <title> 提取
        m = re.search(r'<title>(.*?)</title>', r.text)
        if m:
            return m.group(1).replace(' - 酷狗音乐', '').strip()
        # 尝试从 __INITIAL_STATE__ 提取
        m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', r.text, re.DOTALL)
        if m:
            data = json.loads(m.group(1))
            return data.get('playlist', {}).get('info', {}).get('title', pid)
    except:
        pass
    return str(pid)

def get_songs(pid):
    """移动端接口获取歌单全部歌曲"""
    url = 'https://mobilecdn.kugou.com/api/v3/playlist/song'
    params = {'format': 'json', 'playlistid': pid, 'page': 1, 'pagesize': 1000}
    try:
        r = session.get(url, params=params, timeout=15)
        data = r.json()
        if data.get('status') == 1:
            return data['data']['info']
    except:
        pass
    return []

def get_play_url(song):
    """通过 hash 获取高音质播放链接（参考 Kugou-api 项目）"""
    hashes = [song.get('sqhash'), song.get('320hash'), song.get('hash')]
    mid = session.cookies.get('kg_mid', '123456')
    for h in hashes:
        if not h: continue
        params = {'r': 'play/getdata', 'hash': h, 'album_id': song.get('album_id', 0), 'mid': mid}
        try:
            r = session.get('https://wwwapi.kugou.com/yy/index.php',
                            params=params, headers={'Referer': 'https://www.kugou.com'}, timeout=10)
            data = r.json()
            if data.get('err_code') == 0 and data.get('data'):
                url = data['data'].get('play_url') or data['data'].get('url')
                if url: return url
        except:
            continue
    return None

def download(url, path):
    try:
        r = requests.get(url, stream=True, timeout=60)
        r.raise_for_status()
        with open(path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f'✅ 已下载：{path.name}')
        return True
    except Exception as e:
        print(f'❌ 下载失败 {path.name}: {e}')
        if path.exists(): path.unlink()
        return False

def sync():
    ids = [x.strip() for x in PLAYLIST_IDS.split(',') if x.strip()]
    if not ids:
        print('⚠️ 未设置歌单ID，请检查环境变量 PLAYLIST_IDS')
        return
    names = [x.strip() for x in PLAYLIST_NAMES.split(',') if x.strip()] if PLAYLIST_NAMES else []

    for i, pid in enumerate(ids):
        name = names[i] if i < len(names) else get_playlist_name(pid)
        print(f'\n🎵 同步歌单：{name} ({pid})')
        folder = Path(DOWNLOAD_DIR) / safe_name(name)
        folder.mkdir(parents=True, exist_ok=True)

        songs = get_songs(pid)
        for song in songs:
            songname = song.get('songname', '未知')
            singer   = song.get('singername', '未知')
            filename = safe_name(f'{singer} - {songname}.mp3')
            filepath = folder / filename
            if filepath.exists():
                print(f'⏭️ 跳过（已存在）：{filename}')
                continue
            url = get_play_url(song)
            if url:
                download(url, filepath)
            else:
                print(f'🔇 无法获取链接：{filename}')

if __name__ == '__main__':
    print('🚀 酷狗歌单同步服务启动')
    sync()
    schedule.every(INTERVAL_MIN).minutes.do(sync)
    while True:
        schedule.run_pending()
        time.sleep(30)
