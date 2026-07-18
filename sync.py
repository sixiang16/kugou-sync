#!/usr/bin/env python3
"""酷狗全歌单自动同步下载服务 - 适用于飞牛 NAS Docker"""
import os, re, json, time, requests, schedule
from pathlib import Path

# ----- 配置（全部从环境变量读取） -----
DOWNLOAD_DIR    = os.getenv('DOWNLOAD_DIR', '/music')
KUGOU_COOKIE    = os.getenv('KUGOU_COOKIE', '')
INTERVAL_MIN    = int(os.getenv('INTERVAL_MIN', '60'))

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})
if KUGOU_COOKIE:
    for item in KUGOU_COOKIE.split(';'):
        if '=' in item:
            k, v = item.strip().split('=', 1)
            session.cookies.set(k, v)

def safe_name(text):
    """清理文件名中的非法字符"""
    return re.sub(r'[\\/*?:"<>|]', '_', text)

def get_songs(pid):
    """通过移动端接口获取歌单全部歌曲"""
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
    """通过 hash 获取高音质播放链接"""
    hashes = [song.get('sqhash'), song.get('320hash'), song.get('hash')]
    mid = session.cookies.get('kg_mid', '123456')
    for h in hashes:
        if not h:
            continue
        params = {
            'r': 'play/getdata',
            'hash': h,
            'album_id': song.get('album_id', 0),
            'mid': mid
        }
        try:
            r = session.get(
                'https://wwwapi.kugou.com/yy/index.php',
                params=params,
                headers={'Referer': 'https://www.kugou.com'},
                timeout=10
            )
            data = r.json()
            if data.get('err_code') == 0 and data.get('data'):
                url = data['data'].get('play_url') or data['data'].get('url')
                if url:
                    return url
        except:
            continue
    return None

def download(url, path):
    """下载音乐文件"""
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
        if path.exists():
            path.unlink()
        return False

def get_user_playlists():
    """自动获取当前账号的所有歌单（包括私有和活动添加的）"""
    url = 'https://mobilecdn.kugou.com/api/v3/playlist/getsonglist'
    params = {'format': 'json', 'pagesize': 500, 'page': 1}
    try:
        r = session.get(url, params=params, timeout=15,
                        headers={'Referer': 'https://m.kugou.com'})
        data = r.json()
        if data.get('status') == 1:
            return data['data']['info']
    except:
        pass
    return []

def sync_all():
    """同步所有歌单"""
    playlists = get_user_playlists()
    if not playlists:
        print('⚠️ 未能获取到任何歌单，请检查 Cookie 是否有效')
        return

    for pl in playlists:
        pid = pl['playlistid']
        name = pl.get('title', str(pid))
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
    print('🚀 酷狗全歌单同步服务启动')
    sync_all()
    schedule.every(INTERVAL_MIN).minutes.do(sync_all)
    while True:
        schedule.run_pending()
        time.sleep(30)
