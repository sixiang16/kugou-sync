#!/usr/bin/env python3
"""
酷狗歌单同步服务 + Web 控制面板
- 使用 musicapi 核心接口获取歌单歌曲，不依赖不稳定 API
- 手动添加/管理要同步的歌单 (通过 specialid)
- 支持定时全量同步、手动触发、实时日志
"""
import os, re, json, time, threading, sys, logging, traceback, hashlib
from pathlib import Path

import requests
from flask import Flask, render_template, request, jsonify
import schedule

# ============================================================
# 日志
# ============================================================
LOG_FILE = '/tmp/sync.log'
ERROR_LOG_FILE = '/tmp/error.log'
logger = logging.getLogger('kugou_sync')
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
error_logger = logging.getLogger('error_logger')
error_logger.setLevel(logging.ERROR)
error_handler = logging.FileHandler(ERROR_LOG_FILE, mode='a', encoding='utf-8')
error_handler.setFormatter(formatter)
error_logger.addHandler(error_handler)
def log_info(msg): logger.info(msg)
def log_error(msg): logger.error(msg); error_logger.error(msg)

# ============================================================
# 配置
# ============================================================
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', '/music')
WEB_PORT    = int(os.getenv('WEB_PORT', '5000'))
INTERVAL_MIN = int(os.getenv('INTERVAL_MIN', '60'))

app = Flask(__name__)

# ============================================================
# 歌单配置文件 (存储到文件，避免每次重置)
# ============================================================
PLAYLIST_STORE = '/app/data/playlists.json'   # 在容器内保存用户添加的歌单

def load_playlists():
    try:
        with open(PLAYLIST_STORE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return []

def save_playlists(plist):
    os.makedirs(os.path.dirname(PLAYLIST_STORE), exist_ok=True)
    with open(PLAYLIST_STORE, 'w', encoding='utf-8') as f:
        json.dump(plist, f, ensure_ascii=False, indent=2)

# ============================================================
# musicapi 签名函数
# ============================================================
def kugou_music_sign(url):
    uri = url.split('?')[1]
    uri_list = uri.split('&')
    ordered_list = sorted(uri_list)
    uri = 'OIlwieks28dk2k092lksi2UIkp' + "".join(ordered_list) + 'OIlwieks28dk2k092lksi2UIkp'
    return hashlib.md5(uri.encode()).hexdigest()

# ============================================================
# 获取歌单歌曲 (来自 musicapi 的 get_kugou_list)
# ============================================================
def get_songs(pid):
    url = (f'http://gatewayretry.kugou.com/v2/get_other_list_file'
           f'?specialid={pid}&need_sort=1&module=CloudMusic&clientver=11239&pagesize=300'
           f'&specalidpgc={pid}&userid=0&page=1&type=0&area_code=1&appid=1005')
    headers = {
        'User-Agent': 'Android9-AndroidPhone-11239-18-0-playlist-wifi',
        'Host': 'gatewayretry.kugou.com',
        'x-router': 'pubsongscdn.kugou.com',
        'mid': '239526275778893399526700786998289824956',
        'dfid': '-',
        'clienttime': str(int(time.time()))
    }
    signature = kugou_music_sign(url)
    full_url = url + '&signature=' + signature
    try:
        r = requests.get(full_url, headers=headers, timeout=15)
        data = r.json()
        if data.get('status') == 1 and data.get('data'):
            songs = []
            for item in data['data']['info']:
                parts = item['name'].split(' - ', 1)
                singer = parts[0] if len(parts) > 1 else ''
                songname = parts[1] if len(parts) > 1 else item['name']
                songs.append({
                    'songname': songname,
                    'singername': singer,
                    'hash': item.get('hash', ''),
                    'sqhash': item.get('sqhash', ''),
                    '320hash': item.get('320hash', ''),
                    'album_id': item.get('album_id', 0)
                })
            return songs
        else:
            log_error(f'获取歌单歌曲失败 pid={pid}: {data}')
    except Exception as e:
        log_error(f'获取歌单歌曲异常 pid={pid}: {e}')
    return []

def get_play_url(song):
    """通过 hash 获取高音质链接 (需要 Cookie 中的 kg_mid)"""
    cookie_str = os.getenv('KUGOU_COOKIE', '')
    kg_mid = '123456'
    if 'kg_mid=' in cookie_str:
        kg_mid = cookie_str.split('kg_mid=')[1].split(';')[0]
    hashes = [song.get('sqhash'), song.get('320hash'), song.get('hash')]
    for h in hashes:
        if not h: continue
        params = {
            'r': 'play/getdata',
            'hash': h,
            'album_id': song.get('album_id', 0),
            'mid': kg_mid
        }
        try:
            r = requests.get('https://wwwapi.kugou.com/yy/index.php',
                             params=params,
                             headers={'Referer': 'https://www.kugou.com'},
                             cookies={'kg_mid': kg_mid},
                             timeout=10, verify=False)
            data = r.json()
            if data.get('err_code') == 0 and data.get('data'):
                url = data['data'].get('play_url') or data['data'].get('url')
                if url: return url
        except: continue
    return None

def download_file(url, filepath):
    try:
        r = requests.get(url, stream=True, timeout=60, verify=False)
        r.raise_for_status()
        with open(filepath, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        log_info(f'✅ 已下载：{filepath.name}')
        return True
    except Exception as e:
        log_error(f'❌ 下载失败 {filepath.name}: {e}')
        if filepath.exists(): filepath.unlink()
        return False

def safe_name(text):
    return re.sub(r'[\\/*?:"<>|]', '_', str(text))

# ============================================================
# 同步逻辑 (针对已保存的歌单)
# ============================================================
def sync_playlist(pid, name):
    folder = Path(DOWNLOAD_DIR) / safe_name(name)
    folder.mkdir(parents=True, exist_ok=True)
    songs = get_songs(pid)
    if not songs:
        log_error(f'歌单 {name} ({pid}) 没有歌曲或获取失败')
        return
    for song in songs:
        filename = safe_name(f'{song["singername"]} - {song["songname"]}.mp3')
        filepath = folder / filename
        if filepath.exists():
            log_info(f'⏭️ 跳过：{filename}')
            continue
        url = get_play_url(song)
        if url:
            download_file(url, filepath)
        else:
            log_info(f'🔇 无法获取链接：{filename}')

def sync_all():
    playlists = load_playlists()
    if not playlists:
        log_info('⚠️ 没有待同步的歌单，请通过面板添加')
        return
    log_info('⏰ 定时同步开始')
    for pl in playlists:
        pid = pl.get('id')
        name = pl.get('name', '未知歌单')
        log_info(f'🎵 同步歌单：{name} ({pid})')
        try:
            sync_playlist(pid, name)
        except Exception as e:
            log_error(f'同步歌单 {name} 出错: {e}')
    log_info('✅ 定时同步完成')

def run_scheduler():
    if INTERVAL_MIN > 0:
        schedule.every(INTERVAL_MIN).minutes.do(sync_all)
        log_info(f'📅 定时同步已设置，间隔 {INTERVAL_MIN} 分钟')
        while True:
            schedule.run_pending()
            time.sleep(30)

# ============================================================
# Web 路由
# ============================================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/playlists', methods=['GET'])
def api_playlists():
    return jsonify(load_playlists())

@app.route('/api/playlists', methods=['POST'])
def api_add_playlist():
    data = request.get_json()
    pid = data.get('id', '').strip()
    name = data.get('name', '').strip()
    if not pid:
        return jsonify({'error': '歌单ID不能为空'}), 400
    playlists = load_playlists()
    # 去重
    if any(p['id'] == pid for p in playlists):
        return jsonify({'error': '歌单已存在'}), 409
    playlists.append({'id': pid, 'name': name if name else pid})
    save_playlists(playlists)
    log_info(f'➕ 添加歌单：{name or pid} ({pid})')
    return jsonify({'message': '添加成功', 'playlist': {'id': pid, 'name': name or pid}})

@app.route('/api/playlists/<pid>', methods=['DELETE'])
def api_delete_playlist(pid):
    playlists = load_playlists()
    new_list = [p for p in playlists if p['id'] != pid]
    if len(new_list) == len(playlists):
        return jsonify({'error': '歌单不存在'}), 404
    save_playlists(new_list)
    log_info(f'🗑️ 删除歌单：{pid}')
    return jsonify({'message': '删除成功'})

@app.route('/api/logs', methods=['GET'])
def api_logs():
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            return jsonify([line.rstrip('\n') for line in lines[-200:]])
    except:
        return jsonify([])

@app.route('/api/sync', methods=['POST'])
def api_sync():
    data = request.get_json()
    if data.get('all'):
        t = threading.Thread(target=sync_all, daemon=True)
        t.start()
        return jsonify({'message': '全量同步已启动'})
    ids = data.get('ids')
    if not isinstance(ids, list) or len(ids) == 0:
        return jsonify({'error': 'ids 必须是非空数组'}), 400
    playlists = load_playlists()
    id_name = {p['id']: p['name'] for p in playlists}
    for pid in ids:
        name = id_name.get(pid, pid)
        t = threading.Thread(target=sync_playlist, args=(pid, name), daemon=True)
        t.start()
    return jsonify({'message': f'已开始同步 {len(ids)} 个歌单'})

# ============================================================
# 启动
# ============================================================
if __name__ == '__main__':
    try:
        log_info('🚀 酷狗歌单同步服务启动（手动歌单模式）')
        threading.Thread(target=run_scheduler, daemon=True).start()
        log_info(f'🌐 面板监听 http://0.0.0.0:{WEB_PORT}')
        app.run(host='0.0.0.0', port=WEB_PORT, debug=False, use_reloader=False)
    except Exception as e:
        log_error(f'💥 启动失败: {traceback.format_exc()}')
        with open(ERROR_LOG_FILE, 'a') as f:
            f.write(f'=== 启动失败 {time.strftime("%Y-%m-%d %H:%M:%S")} ===\n')
            traceback.print_exc(file=f)
        while True: time.sleep(60)
