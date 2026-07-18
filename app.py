#!/usr/bin/env python3
"""
酷狗全歌单同步服务 + Web 控制面板（稳定版）
- 自动获取所有歌单
- 前端控制面板（选择歌单同步、查看实时日志）
- 定时全量同步
- 启动失败自动写错误日志，容器保持运行避免无限重启
"""

import os, re, json, time, threading, sys, logging, traceback
from pathlib import Path

import requests
from flask import Flask, render_template, request, jsonify
import schedule

# ============================================================
# 日志配置（同时输出到文件和 stdout，保证 docker logs 可见）
# ============================================================
LOG_FILE = '/tmp/sync.log'
ERROR_LOG_FILE = '/tmp/error.log'

# 创建一个 logger 同时写文件和 stdout
logger = logging.getLogger('kugou_sync')
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')

# 文件 handler
file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# 控制台 handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# 专门用于错误日志的文件
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
# 全局配置（从环境变量读取）
# ============================================================
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', '/music')
KUGOU_COOKIE = os.getenv('KUGOU_COOKIE', '')
INTERVAL_MIN = int(os.getenv('INTERVAL_MIN', '60'))
WEB_PORT    = int(os.getenv('WEB_PORT', '5000'))

# ============================================================
# Flask 应用初始化
# ============================================================
app = Flask(__name__)

# ============================================================
# Session 初始化（携带 Cookie）
# ============================================================
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
})
if KUGOU_COOKIE:
    for item in KUGOU_COOKIE.split(';'):
        item = item.strip()
        if '=' in item:
            k, v = item.split('=', 1)
            session.cookies.set(k.strip(), v.strip())

# ============================================================
# 工具函数
# ============================================================
def safe_name(text):
    """清理文件名中的非法字符"""
    return re.sub(r'[\\/*?:"<>|]', '_', str(text))

def get_songs(pid):
    """获取歌单内全部歌曲"""
    url = 'https://mobilecdn.kugou.com/api/v3/playlist/song'
    params = {'format': 'json', 'playlistid': pid, 'page': 1, 'pagesize': 1000}
    try:
        r = session.get(url, params=params, timeout=15)
        data = r.json()
        if data.get('status') == 1:
            return data['data']['info']
    except Exception as e:
        log_error(f'获取歌单歌曲失败 pid={pid}: {e}')
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
        except Exception as e:
            log_error(f'获取播放链接失败 hash={h}: {e}')
    return None

def download_file(url, filepath):
    """下载单个文件并记录日志"""
    try:
        r = requests.get(url, stream=True, timeout=60)
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
# 歌单获取（多接口回退，自动提取 userid）
# ============================================================
def get_user_playlists():
    """自动获取当前账号的所有歌单（移动端接口优先，PC端备用）"""
    # 提取 userId
    userid = session.cookies.get('KugooID', '').strip()
    if not userid:
        # 尝试从 Cookie 字符串中正则提取 KugooID=xxx
        match = re.search(r'KugooID=(\d+)', KUGOU_COOKIE)
        if match:
            userid = match.group(1)
    if not userid:
        log_error('⚠️ Cookie 中未找到 KugooID，无法获取歌单列表')
        return []

    # 移动端接口
    try:
        url = 'https://mobilecdn.kugou.com/api/v3/playlist/getsonglist'
        params = {'format': 'json', 'userid': userid, 'page': 1, 'pagesize': 500}
        r = session.get(url, params=params, timeout=15,
                        headers={'Referer': 'https://m.kugou.com'})
        data = r.json()
        if data.get('status') == 1 and data['data'].get('info'):
            log_info(f'✅ 通过移动端接口获取到 {len(data["data"]["info"])} 个歌单')
            return data['data']['info']
        else:
            log_error(f'移动端接口返回异常: {data}')
    except Exception as e:
        log_error(f'移动端接口请求失败: {e}')

    # PC端备用接口
    try:
        url_pc = 'https://wwwapi.kugou.com/yy/index.php'
        params_pc = {'r': 'play/getUserPlaylist', 'uid': userid}
        r = session.get(url_pc, params=params_pc, timeout=15,
                        headers={'Referer': 'https://www.kugou.com'})
        data = r.json()
        if data.get('err_code') == 0 and data.get('data'):
            # PC端返回数据格式可能是 list 或 dict
            playlist_data = data.get('data')
            if isinstance(playlist_data, dict):
                playlist_list = playlist_data.get('list', [])
            else:
                playlist_list = playlist_data
            log_info(f'✅ 通过PC端接口获取到 {len(playlist_list)} 个歌单')
            return playlist_list
        else:
            log_error(f'PC端接口返回异常: {data}')
    except Exception as e:
        log_error(f'PC端接口请求失败: {e}')

    log_error('⛔ 所有接口均未能获取歌单，请确认 Cookie 有效且账号有歌单')
    return []

# ============================================================
# 同步任务（后台线程）
# ============================================================
def sync_selected_playlists(ids):
    """手动同步指定的歌单 ID 列表"""
    log_info(f'📢 手动同步开始，共 {len(ids)} 个歌单')
    playlists = get_user_playlists()
    id_name = {pl['playlistid']: pl.get('title', pl['playlistid']) for pl in playlists}
    if not id_name:
        log_error('无法获取歌单名称映射')
    for pid in ids:
        name = id_name.get(pid, str(pid))
        log_info(f'🎵 正在同步歌单：{name} ({pid})')
        folder = Path(DOWNLOAD_DIR) / safe_name(name)
        folder.mkdir(parents=True, exist_ok=True)
        songs = get_songs(pid)
        if not songs:
            log_error(f'歌单 {name} 中没有歌曲或获取失败')
            continue
        for song in songs:
            songname = song.get('songname', '未知')
            singer   = song.get('singername', '未知')
            filename = safe_name(f'{singer} - {songname}.mp3')
            filepath = folder / filename
            if filepath.exists():
                log_info(f'⏭️ 跳过（已存在）：{filename}')
                continue
            url = get_play_url(song)
            if url:
                download_file(url, filepath)
            else:
                log_info(f'🔇 无法获取链接：{filename}')
    log_info('✅ 手动同步完成')

def sync_all():
    """全量同步所有歌单（定时任务）"""
    log_info('⏰ 定时全量同步开始')
    playlists = get_user_playlists()
    if not playlists:
        log_error('⚠️ 未能获取到任何歌单，跳过本次定时同步')
        return
    for pl in playlists:
        pid = pl['playlistid']
        name = pl.get('title', str(pid))
        log_info(f'🎵 正在同步歌单：{name} ({pid})')
        folder = Path(DOWNLOAD_DIR) / safe_name(name)
        folder.mkdir(parents=True, exist_ok=True)
        songs = get_songs(pid)
        if not songs:
            continue
        for song in songs:
            songname = song.get('songname', '未知')
            singer   = song.get('singername', '未知')
            filename = safe_name(f'{singer} - {songname}.mp3')
            filepath = folder / filename
            if filepath.exists():
                log_info(f'⏭️ 跳过：{filename}')
                continue
            url = get_play_url(song)
            if url:
                download_file(url, filepath)
            else:
                log_info(f'🔇 无法获取链接：{filename}')
    log_info('✅ 定时全量同步完成')

# ============================================================
# 定时任务后台运行
# ============================================================
def run_scheduler():
    if INTERVAL_MIN > 0:
        schedule.every(INTERVAL_MIN).minutes.do(sync_all)
        log_info(f'📅 定时同步已设置，间隔 {INTERVAL_MIN} 分钟')
        while True:
            schedule.run_pending()
            time.sleep(30)

# ============================================================
# Flask 路由
# ============================================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/playlists')
def api_playlists():
    pls = get_user_playlists()
    result = [{'id': pl['playlistid'], 'name': pl.get('title', '未知歌单')} for pl in pls]
    return jsonify(result)

@app.route('/api/logs')
def api_logs():
    """返回日志文件最后 200 行"""
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            # 返回最近 200 行
            return jsonify([line.rstrip('\n') for line in lines[-200:]])
    except FileNotFoundError:
        return jsonify([])

@app.route('/api/sync', methods=['POST'])
def api_sync():
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

# ============================================================
# 主入口（防崩溃 + 保活）
# ============================================================
if __name__ == '__main__':
    try:
        log_info('🚀 酷狗全歌单同步服务准备启动')
        # 启动定时任务线程
        threading.Thread(target=run_scheduler, daemon=True).start()
        # 启动 Web 服务
        log_info(f'🌐 Web 面板正在监听 http://0.0.0.0:{WEB_PORT}')
        app.run(host='0.0.0.0', port=WEB_PORT, debug=False, use_reloader=False)
    except Exception as e:
        error_msg = f'💥 启动失败: {traceback.format_exc()}'
        log_error(error_msg)
        # 将错误写入错误日志文件
        with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'=== 启动失败 {time.strftime("%Y-%m-%d %H:%M:%S")} ===\n')
            traceback.print_exc(file=f)
        # 保持容器不退出（无限循环，方便进入容器查看）
        print(f'FATAL: 启动失败，进入保活模式。查看错误日志: {ERROR_LOG_FILE}', flush=True)
        while True:
            time.sleep(60)
