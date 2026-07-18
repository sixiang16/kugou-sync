#!/usr/bin/env python3
"""酷狗全歌单同步服务 + Web 控制面板"""
import os, re, json, time, threading
import requests
from flask import Flask, render_template, request, jsonify
from pathlib import Path
import schedule

# ---------- 全局配置 ----------
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', '/music')
KUGOU_COOKIE = os.getenv('KUGOU_COOKIE', '')
INTERVAL_MIN = int(os.getenv('INTERVAL_MIN', '60'))
WEB_PORT    = int(os.getenv('WEB_PORT', '5000'))

app = Flask(__name__)

# ---------- 日志系统（线程安全） ----------
log_lock = threading.Lock()
sync_logs = []         # 每条日志 {"id": 递增, "time": "HH:MM:SS", "message": str}
log_counter = 0
MAX_LOG_LINES = 500

def add_log(message):
    global log_counter
    with log_lock:
        log_counter += 1
        sync_logs.append({
            "id": log_counter,
            "time": time.strftime("%H:%M:%S"),
            "message": message
        })
        if len(sync_logs) > MAX_LOG_LINES:
            sync_logs.pop(0)
    print(f"[{sync_logs[-1]['time']}] {message}")

# ---------- 网络会话（登录 Cookie） ----------
session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})
if KUGOU_COOKIE:
    for item in KUGOU_COOKIE.split(';'):
        if '=' in item:
            k, v = item.strip().split('=', 1)
            session.cookies.set(k, v)

# ---------- 工具函数 ----------
def safe_name(text):
    return re.sub(r'[\\/*?:"<>|]', '_', text)

def get_songs(pid):
    """获取歌单内全部歌曲"""
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
    """获取歌曲高音质下载链接"""
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
            r = session.get('https://wwwapi.kugou.com/yy/index.php',
                            params=params,
                            headers={'Referer': 'https://www.kugou.com'},
                            timeout=10)
            data = r.json()
            if data.get('err_code') == 0 and data.get('data'):
                url = data['data'].get('play_url') or data['data'].get('url')
                if url:
                    return url
        except:
            continue
    return None

def download(url, path):
    """下载单个文件并记录日志"""
    try:
        r = requests.get(url, stream=True, timeout=60)
        r.raise_for_status()
        with open(path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        add_log(f'✅ 已下载：{path.name}')
        return True
    except Exception as e:
        add_log(f'❌ 下载失败 {path.name}: {e}')
        if path.exists():
            path.unlink()
        return False

def get_user_playlists():
    """自动获取当前账号所有歌单"""
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

# ---------- 同步任务（后台线程中执行） ----------

def sync_selected_playlists(ids):
    """手动同步指定的歌单 ID 列表"""
    add_log(f'📢 手动同步开始，共 {len(ids)} 个歌单')
    playlists = get_user_playlists()
    id_name = {pl['playlistid']: pl.get('title', pl['playlistid']) for pl in playlists}

    for pid in ids:
        name = id_name.get(pid, pid)
        add_log(f'🎵 同步歌单：{name} ({pid})')
        folder = Path(DOWNLOAD_DIR) / safe_name(name)
        folder.mkdir(parents=True, exist_ok=True)

        songs = get_songs(pid)
        for song in songs:
            songname = song.get('songname', '未知')
            singer   = song.get('singername', '未知')
            filename = safe_name(f'{singer} - {songname}.mp3')
            filepath = folder / filename
            if filepath.exists():
                add_log(f'⏭️ 跳过（已存在）：{filename}')
                continue
            url = get_play_url(song)
            if url:
                download(url, filepath)
            else:
                add_log(f'🔇 无法获取链接：{filename}')
    add_log('✅ 手动同步完成')

def sync_all():
    """全量同步所有歌单（定时任务使用）"""
    add_log('⏰ 定时全量同步开始')
    playlists = get_user_playlists()
    if not playlists:
        add_log('⚠️ 未能获取到任何歌单，请检查 Cookie 是否有效')
        return

    for pl in playlists:
        pid = pl['playlistid']
        name = pl.get('title', str(pid))
        add_log(f'🎵 同步歌单：{name} ({pid})')
        folder = Path(DOWNLOAD_DIR) / safe_name(name)
        folder.mkdir(parents=True, exist_ok=True)

        songs = get_songs(pid)
        for song in songs:
            songname = song.get('songname', '未知')
            singer   = song.get('singername', '未知')
            filename = safe_name(f'{singer} - {songname}.mp3')
            filepath = folder / filename
            if filepath.exists():
                add_log(f'⏭️ 跳过：{filename}')
                continue
            url = get_play_url(song)
            if url:
                download(url, filepath)
            else:
                add_log(f'🔇 无法获取链接：{filename}')
    add_log('✅ 定时全量同步完成')

# ---------- Flask 路由 ----------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/playlists')
def api_playlists():
    """返回所有歌单基本信息"""
    pls = get_user_playlists()
    result = [{'id': pl['playlistid'], 'name': pl.get('title', '未知歌单')} for pl in pls]
    return jsonify(result)

@app.route('/api/logs')
def api_logs():
    """返回全部日志（前端根据 id 增量更新）"""
    with log_lock:
        logs_snapshot = list(sync_logs)
    return jsonify(logs_snapshot)

@app.route('/api/sync', methods=['POST'])
def api_sync():
    """手动触发同步"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请求数据为空'}), 400

    if data.get('all'):
        t = threading.Thread(target=sync_all, daemon=True)
        t.start()
        return jsonify({'message': '全量同步任务已启动'})

    ids = data.get('ids')
    if not isinstance(ids, list) or len(ids) == 0:
        return jsonify({'error': 'ids 必须是非空数组'}), 400

    t = threading.Thread(target=sync_selected_playlists, args=(ids,), daemon=True)
    t.start()
    return jsonify({'message': f'已开始同步 {len(ids)} 个歌单'})

# ---------- 启动定时任务 ----------
def run_scheduler():
    if INTERVAL_MIN > 0:
        schedule.every(INTERVAL_MIN).minutes.do(sync_all)
        add_log(f'📅 定时同步已设置，间隔 {INTERVAL_MIN} 分钟')
        while True:
            schedule.run_pending()
            time.sleep(30)

if __name__ == '__main__':
    # 后台定时线程
    threading.Thread(target=run_scheduler, daemon=True).start()
    add_log('🚀 Web 面板启动就绪')
    app.run(host='0.0.0.0', port=WEB_PORT, debug=False)
