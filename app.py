#!/usr/bin/env python3
"""
酷狗全歌单同步服务 + Web 控制面板
集成 musicapi 的歌单获取接口，稳定可靠
"""
import os, re, json, time, threading, sys, logging, traceback, hashlib
from pathlib import Path

import requests
from flask import Flask, render_template, request, jsonify
import schedule
import urllib3

# ============================================================
# 关闭 SSL 警告（保证移动端接口不报错）
# ============================================================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# 日志配置（输出到 stdout 和文件）
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

def log_info(msg):
    logger.info(msg)

def log_error(msg):
    logger.error(msg)
    error_logger.error(msg)

# ============================================================
# 全局配置
# ============================================================
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', '/music')
KUGOU_COOKIE = os.getenv('KUGOU_COOKIE', '')
INTERVAL_MIN = int(os.getenv('INTERVAL_MIN', '60'))
WEB_PORT    = int(os.getenv('WEB_PORT', '5000'))

app = Flask(__name__)

# ============================================================
# 网络会话（携带 Cookie，关闭 SSL 验证）
# ============================================================
session = requests.Session()
session.verify = False
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
})

if KUGOU_COOKIE:
    for item in KUGOU_COOKIE.split(';'):
        item = item.strip()
        if '=' in item:
            k, v = item.split('=', 1)
            session.cookies.set(k.strip(), v.strip())

# ============================================================
# musicapi 移植：酷狗签名函数（与 musicapi 完全一致）
# ============================================================
def kugou_music_sign(url):
    uri = url.split('?')[1]
    uri_list = uri.split('&')
    ordered_list = sorted(uri_list)
    uri = 'OIlwieks28dk2k092lksi2UIkp' + "".join(ordered_list) + 'OIlwieks28dk2k092lksi2UIkp'
    return hashlib.md5(uri.encode(encoding='utf-8')).hexdigest()

# ============================================================
# 歌单歌曲获取（基于 musicapi 的 gatewayretry 接口，稳定可靠）
# ============================================================
def get_songs(pid):
    """
    通过 gatewayretry.kugou.com 获取歌单内全部歌曲
    该接口来自 musicapi，已内置签名算法，无需 Cookie，稳定性高
    """
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
                # 分离歌手和歌曲名（格式：歌手 - 歌名）
                parts = item['name'].split(' - ', 1)
                singer = parts[0] if len(parts) > 1 else ''
                songname = parts[1] if len(parts) > 1 else item['name']
                songs.append({
                    'songname': songname,
                    'singername': singer,
                    'hash': item.get('hash', ''),
                    'sqhash': item.get('sqhash', ''),
                    '320hash': item.get('320hash', ''),
                    'album_id': item.get('album_id', 0),
                    'cover': item.get('cover', '')
                })
            return songs
        else:
            log_error(f'获取歌单歌曲异常 pid={pid}: {data}')
    except Exception as e:
        log_error(f'获取歌单歌曲失败 pid={pid}: {e}')
    return []

# ============================================================
# 下载链接获取（保留原有 hash 方式，稳定可靠）
# ============================================================
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
            r = requests.get(
                'https://wwwapi.kugou.com/yy/index.php',
                params=params,
                headers={'Referer': 'https://www.kugou.com'},
                cookies=session.cookies,
                timeout=10,
                verify=False
            )
            data = r.json()
            if data.get('err_code') == 0 and data.get('data'):
                url = data['data'].get('play_url') or data['data'].get('url')
                if url:
                    return url
        except Exception as e:
            log_error(f'获取播放链接失败 hash={h}: {e}')
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
        if filepath.exists():
            filepath.unlink()
        return False

# ============================================================
# 用户歌单列表获取（用原有的移动端接口，已关闭 SSL 验证）
# ============================================================
def get_user_playlists():
    userid = session.cookies.get('KugooID', '').strip()
    if not userid:
        match = re.search(r'KugooID=(\d+)', KUGOU_COOKIE)
        if match:
            userid = match.group(1)
    if not userid:
        log_error('⚠️ Cookie 中未找到 KugooID，无法获取歌单')
        return []

    # 移动端接口（原接口，已通过 session.verify=False 关闭 SSL 验证）
    try:
        url = 'https://mobilecdn.kugou.com/api/v3/playlist/getsonglist'
        params = {'format': 'json', 'userid': userid, 'page': 1, 'pagesize': 500}
        r = session.get(url, params=params, timeout=15,
                        headers={'Referer': 'https://m.kugou.com'})
        data = r.json()
        if data.get('status') == 1 and data['data'].get('info'):
            playlists = data['data']['info']
            log_info(f'✅ 获取到 {len(playlists)} 个歌单')
            return playlists
        else:
            log_error(f'移动端接口返回异常: {data}')
    except Exception as e:
        log_error(f'移动端接口失败: {e}')

    # PC端备用接口（增强请求头）
    try:
        url_pc = 'https://wwwapi.kugou.com/yy/index.php'
        params_pc = {'r': 'play/getUserPlaylist', 'uid': userid, 'page': 1, 'pagesize': 500}
        r = session.get(url_pc, params=params_pc, timeout=15,
                        headers={'Referer': 'https://www.kugou.com'})
        data = r.json()
        if data.get('err_code') == 0 and data.get('data'):
            pl_data = data['data']
            if isinstance(pl_data, dict):
                pl_list = pl_data.get('list', [])
            else:
                pl_list = pl_data
            log_info(f'✅ PC端获取到 {len(pl_list)} 个歌单')
            return pl_list
        else:
            log_error(f'PC端接口返回异常: {data}')
    except Exception as e:
        log_error(f'PC端接口失败: {e}')

    log_error('⛔ 所有接口均未能获取歌单')
    return []

# ============================================================
# 同步任务
# ============================================================
def safe_name(text):
    return re.sub(r'[\\/*?:"<>|]', '_', str(text))

def sync_selected_playlists(ids):
    log_info(f'📢 手动同步开始，共 {len(ids)} 个歌单')
    playlists = get_user_playlists()
    id_name = {pl['playlistid']: pl.get('title', str(pl['playlistid'])) for pl in playlists if 'playlistid' in pl}
    for pid in ids:
        name = id_name.get(pid, str(pid))
        log_info(f'🎵 同步歌单：{name} ({pid})')
        folder = Path(DOWNLOAD_DIR) / safe_name(name)
        folder.mkdir(parents=True, exist_ok=True)

        songs = get_songs(pid)
        if not songs:
            continue
        for song in songs:
            filepath = folder / safe_name(f'{song["singername"]} - {song["songname"]}.mp3')
            if filepath.exists():
                log_info(f'⏭️ 跳过：{filepath.name}')
                continue
            url = get_play_url(song)
            if url:
                download_file(url, filepath)
            else:
                log_info(f'🔇 无法获取链接：{filepath.name}')
    log_info('✅ 手动同步完成')

def sync_all():
    log_info('⏰ 定时全量同步开始')
    playlists = get_user_playlists()
    if not playlists:
        return
    for pl in playlists:
        pid = pl.get('playlistid')
        if not pid:
            continue
        name = pl.get('title', str(pid))
        log_info(f'🎵 同步歌单：{name} ({pid})')
        folder = Path(DOWNLOAD_DIR) / safe_name(name)
        folder.mkdir(parents=True, exist_ok=True)

        songs = get_songs(pid)
        if not songs:
            continue
        for song in songs:
            filepath = folder / safe_name(f'{song["singername"]} - {song["songname"]}.mp3')
            if filepath.exists():
                log_info(f'⏭️ 跳过：{filepath.name}')
                continue
            url = get_play_url(song)
            if url:
                download_file(url, filepath)
            else:
                log_info(f'🔇 无法获取链接：{filepath.name}')
    log_info('✅ 定时全量同步完成')

# ============================================================
# 定时后台线程
# ============================================================
def run_scheduler():
    if INTERVAL_MIN > 0:
        schedule.every(INTERVAL_MIN).minutes.do(sync_all)
        log_info(f'📅 定时同步已设置，间隔 {INTERVAL_MIN} 分钟')
        while True:
            schedule.run_pending()
            time.sleep(30)

# ============================================================
# Web 面板路由（不变）
# ============================================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/playlists')
def api_playlists():
    pls = get_user_playlists()
    result = [{'id': pl['playlistid'], 'name': pl.get('title', '未知歌单')} for pl in pls if 'playlistid' in pl]
    return jsonify(result)

@app.route('/api/logs')
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
    if not data:
        return jsonify({'error': '请求数据为空'}), 400
    if data.get('all'):
        t = threading.Thread(target=sync_all, daemon=True)
        t.start()
        return jsonify({'message': '全量同步已启动'})
    ids = data.get('ids')
    if not isinstance(ids, list) or len(ids) == 0:
        return jsonify({'error': 'ids 必须是非空数组'}), 400
    t = threading.Thread(target=sync_selected_playlists, args=(ids,), daemon=True)
    t.start()
    return jsonify({'message': f'已开始同步 {len(ids)} 个歌单'})

# ============================================================
# 主入口
# ============================================================
if __name__ == '__main__':
    try:
        log_info('🚀 酷狗全歌单同步服务启动')
        threading.Thread(target=run_scheduler, daemon=True).start()
        log_info(f'🌐 Web 面板监听 http://0.0.0.0:{WEB_PORT}')
        app.run(host='0.0.0.0', port=WEB_PORT, debug=False, use_reloader=False)
    except Exception as e:
        log_error(f'💥 启动失败: {traceback.format_exc()}')
        with open(ERROR_LOG_FILE, 'a') as f:
            f.write(f'=== 启动失败 {time.strftime("%Y-%m-%d %H:%M:%S")} ===\n')
            traceback.print_exc(file=f)
        while True:
            time.sleep(60)
