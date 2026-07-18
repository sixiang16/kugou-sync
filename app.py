#!/usr/bin/env python3
"""
酷狗全歌单同步服务 + Web 控制面板（稳定版）
修复：
- 关闭 SSL 证书验证（解决 mobilecdn.kugou.com 证书不匹配问题）
- PC 端备用接口增强（添加 Referer、User-Agent 参数，提高成功率）
- 全局异常捕获，确保容器即使出错也不会无限重启且日志完整
"""
import os
import re
import json
import time
import threading
import sys
import logging
import traceback
from pathlib import Path

import requests
from flask import Flask, render_template, request, jsonify
import schedule
import urllib3

# ============================================================
# 关闭 SSL 警告（解决某些环境证书验证失败）
# ============================================================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# 日志配置（同时输出到 stdout 和文件）
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

# 错误日志单独文件
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
# Flask 应用
# ============================================================
app = Flask(__name__)

# ============================================================
# 网络会话（携带 Cookie，关闭 SSL 验证）
# ============================================================
session = requests.Session()
session.verify = False  # 不验证 SSL 证书，避免证书错误导致接口失败
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
        else:
            log_error(f'获取歌单歌曲失败 pid={pid}: {data}')
    except Exception as e:
        log_error(f'获取歌单歌曲异常 pid={pid}: {e}')
    return []

def get_play_url(song):
    """获取歌曲高音质下载链接（依次尝试 sqhash, 320hash, hash）"""
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
                play_url = data['data'].get('play_url') or data['data'].get('url')
                if play_url:
                    return play_url
        except Exception as e:
            log_error(f'获取播放链接失败 hash={h}: {e}')
    return None

def download_file(url, filepath):
    """下载音乐文件，失败则删除未完成的文件"""
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
# 歌单获取（自动提取 userid，双接口回退）
# ============================================================
def get_user_playlists():
    """获取当前账号的所有歌单（私有+活动歌单）"""
    # 提取用户 ID
    userid = session.cookies.get('KugooID', '').strip()
    if not userid:
        # 从字符串中正则匹配
        match = re.search(r'KugooID=(\d+)', KUGOU_COOKIE)
        if match:
            userid = match.group(1)
    if not userid:
        log_error('⚠️ Cookie 中未找到 KugooID，无法获取歌单')
        return []

    # 接口1：移动端（原接口，已关闭 SSL 验证）
    try:
        url = 'https://mobilecdn.kugou.com/api/v3/playlist/getsonglist'
        params = {'format': 'json', 'userid': userid, 'page': 1, 'pagesize': 500}
        r = session.get(url, params=params, timeout=15,
                        headers={'Referer': 'https://m.kugou.com'})
        data = r.json()
        if data.get('status') == 1 and data['data'].get('info'):
            playlist_list = data['data']['info']
            log_info(f'✅ 移动端获取到 {len(playlist_list)} 个歌单')
            return playlist_list
        else:
            log_error(f'移动端接口返回异常: {data}')
    except Exception as e:
        log_error(f'移动端接口请求失败: {e}')

    # 接口2：PC 端备用（添加更多参数）
    try:
        url_pc = 'https://wwwapi.kugou.com/yy/index.php'
        params_pc = {
            'r': 'play/getUserPlaylist',
            'uid': userid,
            'page': 1,
            'pagesize': 500
        }
        headers_pc = {
            'Referer': 'https://www.kugou.com',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        r = session.get(url_pc, params=params_pc, timeout=15, headers=headers_pc)
        # 尝试解析 JSON，若响应内容为空或为 HTML 则记录原始文本
        try:
            data = r.json()
        except:
            log_error(f'PC端接口返回非 JSON，原始内容前200字符: {r.text[:200]}')
            return []
        if data.get('err_code') == 0 and data.get('data'):
            playlist_data = data['data']
            # 格式可能是 dict 包含 list，或者直接是数组
            if isinstance(playlist_data, dict):
                playlist_list = playlist_data.get('list', [])
            else:
                playlist_list = playlist_data
            log_info(f'✅ PC端获取到 {len(playlist_list)} 个歌单')
            return playlist_list
        else:
            log_error(f'PC端接口返回异常: {data}')
    except Exception as e:
        log_error(f'PC端接口请求失败: {e}')

    log_error('⛔ 所有接口均未能获取歌单，请检查 Cookie 是否有效或账号是否有歌单')
    return []

# ============================================================
# 同步任务（在后台线程中运行）
# ============================================================
def sync_selected_playlists(ids):
    """手动同步选中的歌单"""
    log_info(f'📢 手动同步开始，共 {len(ids)} 个歌单')
    playlists = get_user_playlists()
    id_name = {}
    for pl in playlists:
        pid = pl.get('playlistid')
        if pid:
            id_name[pid] = pl.get('title', str(pid))

    for pid in ids:
        name = id_name.get(pid, str(pid))
        log_info(f'🎵 开始同步歌单：{name} ({pid})')
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
    """全量同步所有歌单（定时任务调用）"""
    log_info('⏰ 定时全量同步开始')
    playlists = get_user_playlists()
    if not playlists:
        log_error('⚠️ 未能获取到任何歌单，跳过本次定时同步')
        return

    for pl in playlists:
        pid = pl.get('playlistid')
        if not pid:
            continue
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
# 定时任务后台线程
# ============================================================
def run_scheduler():
    if INTERVAL_MIN > 0:
        schedule.every(INTERVAL_MIN).minutes.do(sync_all)
        log_info(f'📅 定时同步已设置，间隔 {INTERVAL_MIN} 分钟')
        while True:
            schedule.run_pending()
            time.sleep(30)

# ============================================================
# Web 面板路由
# ============================================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/playlists')
def api_playlists():
    """返回所有歌单的基本信息（id, name）"""
    pls = get_user_playlists()
    result = []
    for pl in pls:
        pid = pl.get('playlistid')
        if pid:
            result.append({'id': pid, 'name': pl.get('title', '未知歌单')})
    return jsonify(result)

@app.route('/api/logs')
def api_logs():
    """返回日志文件中最后200行"""
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            return jsonify([line.rstrip('\n') for line in lines[-200:]])
    except FileNotFoundError:
        return jsonify([])

@app.route('/api/sync', methods=['POST'])
def api_sync():
    """手动触发同步（全量或部分）"""
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
# 主入口（全局异常捕获，保证容器稳定运行）
# ============================================================
if __name__ == '__main__':
    try:
        log_info('🚀 酷狗全歌单同步服务准备启动')
        # 启动后台定时任务
        threading.Thread(target=run_scheduler, daemon=True).start()
        log_info(f'🌐 Web 面板正在监听 http://0.0.0.0:{WEB_PORT}')
        app.run(host='0.0.0.0', port=WEB_PORT, debug=False, use_reloader=False)
    except Exception as e:
        error_detail = traceback.format_exc()
        log_error(f'💥 启动失败: {error_detail}')
        with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'=== 启动失败 {time.strftime("%Y-%m-%d %H:%M:%S")} ===\n')
            f.write(error_detail)
        # 保持容器运行，方便进入容器查看日志
        print(f'FATAL: 启动失败，进入保活模式。日志文件: {ERROR_LOG_FILE}', flush=True)
        while True:
            time.sleep(60)
