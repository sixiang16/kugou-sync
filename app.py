#!/usr/bin/env python3
"""
酷狗歌单同步服务 + 扫码登录（基于 copyKgSong）
- 扫码登录：生成二维码图片，轮询扫码状态
- 歌单导入：登录成功后调用 copyKgSong 导入全部私人歌单
- 歌曲下载：优先使用 copyKgSong 内置下载，回退到 gateway 接口
"""
import os, sys, re, json, time, threading, logging, hashlib, base64
from pathlib import Path
from io import BytesIO
import requests
from flask import Flask, render_template, request, jsonify, send_file
import schedule

# ---------- 加载 copyKgSong 模块 ----------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'copyKgSong'))
try:
    from api import KuGouApi, KuGouLogin  # 假设主类在 api.py 中，类名可能不同
    # 如果项目使用不同的类/函数，请根据实际调整下面几个属性
    COPY_SONG_AVAILABLE = True
except Exception as e:
    COPY_SONG_AVAILABLE = False
    print(f'copyKgSong 加载失败: {e}')

# ---------- 日志 ----------
LOG_FILE = '/tmp/sync.log'
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', handlers=[
    logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)
])
log = logging.getLogger('kugou_sync')

DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', '/music')
WEB_PORT = int(os.getenv('WEB_PORT', '5000'))
INTERVAL_MIN = int(os.getenv('INTERVAL_MIN', '60'))

app = Flask(__name__)
PLAYLIST_STORE = '/app/data/playlists.json'
# 全局保存登录后的 kg_api 实例（扫码成功后会赋值）
kg_api_instance = None

def load_playlists():
    try:
        with open(PLAYLIST_STORE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return []

def save_playlists(plist):
    os.makedirs(os.path.dirname(PLAYLIST_STORE), exist_ok=True)
    with open(PLAYLIST_STORE, 'w', encoding='utf-8') as f: json.dump(plist, f, ensure_ascii=False, indent=2)

# ---------- 扫码登录相关 ----------
# 用于存储当前扫码请求的临时数据
qr_info = {'key': '', 'image': None, 'status': 'waiting'}  # waiting / success / failed

@app.route('/api/qrcode', methods=['GET'])
def api_qrcode():
    """返回二维码图片（base64）和轮询 key"""
    global kg_api_instance
    if not COPY_SONG_AVAILABLE:
        return jsonify({'error': 'copyKgSong 模块未加载'}), 500
    try:
        login = KuGouLogin()
        # 根据实际 API 调整：获取二维码 key 和图片
        qr_key, qr_image = login.get_qrcode()  # 假设返回 (key, bytes 或 PIL.Image)
        # 如果返回的是 PIL Image，转为 base64
        if hasattr(qr_image, 'tobytes'):
            img_bytes = io.BytesIO()
            qr_image.save(img_bytes, format='PNG')
            img_bytes = img_bytes.getvalue()
        else:
            img_bytes = qr_image
        b64 = base64.b64encode(img_bytes).decode()
        qr_info['key'] = qr_key
        qr_info['image'] = b64
        qr_info['status'] = 'waiting'
        return jsonify({'key': qr_key, 'qr_image': b64})
    except Exception as e:
        log.error(f'获取二维码失败: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/qrcode/check', methods=['POST'])
def api_qrcode_check():
    """轮询扫码状态，成功后保存登录实例"""
    global kg_api_instance
    if not COPY_SONG_AVAILABLE:
        return jsonify({'error': '模块未加载'}), 500
    data = request.get_json()
    key = data.get('key', '')
    if not key or key != qr_info.get('key'):
        return jsonify({'status': 'invalid'}), 400
    try:
        login = KuGouLogin()
        status = login.check_qrcode(key)  # 假设返回 'waiting' / 'success' / 'failed'
        if status == 'success':
            # 扫码成功，初始化全局 API 实例
            kg_api_instance = KuGouApi()
            # 假设需要将 login 中的 cookie 或 token 传给 kg_api_instance
            cookie = login.get_cookie()
            kg_api_instance.set_cookie(cookie)
            qr_info['status'] = 'success'
            log.info('✅ 扫码登录成功')
            return jsonify({'status': 'success'})
        elif status == 'failed':
            qr_info['status'] = 'failed'
            return jsonify({'status': 'failed'})
        else:
            return jsonify({'status': 'waiting'})
    except Exception as e:
        log.error(f'轮询扫码状态失败: {e}')
        return jsonify({'status': 'error', 'msg': str(e)}), 500

# ---------- 歌单导入 ----------
@app.route('/api/import', methods=['POST'])
def api_import():
    global kg_api_instance
    if not COPY_SONG_AVAILABLE:
        return jsonify({'error': 'copyKgSong 模块未加载'}), 500
    if not kg_api_instance:
        return jsonify({'error': '请先扫码登录'}), 401
    try:
        playlists = kg_api_instance.get_all_playlist()  # 根据实际方法调整
        imported = []
        for pl in playlists:
            pid = pl.get('specialid') or pl.get('id') or pl.get('playlistid')
            name = pl.get('title') or pl.get('name', '歌单')
            if pid:
                imported.append({'id': str(pid), 'name': name})
        pls = load_playlists()
        existing_ids = {p['id'] for p in pls}
        new_count = 0
        for item in imported:
            if item['id'] not in existing_ids:
                pls.append(item)
                new_count += 1
        save_playlists(pls)
        log.info(f'🎉 导入成功，新增 {new_count} 个歌单')
        return jsonify({'message': f'导入成功，新增 {new_count} 个歌单', 'playlists': pls})
    except Exception as e:
        log.error(f'导入歌单失败: {e}')
        return jsonify({'error': str(e)}), 500

# ---------- 歌单管理 ----------
@app.route('/api/playlists', methods=['GET'])
def api_playlists():
    return jsonify(load_playlists())

@app.route('/api/playlists/<pid>', methods=['DELETE'])
def api_del(pid):
    pls = [p for p in load_playlists() if p['id'] != pid]
    save_playlists(pls)
    return jsonify({'message': '删除成功'})

# ---------- 歌曲下载同步 ----------
def get_songs(pid):
    """使用 copyKgSong 获取歌单歌曲列表；失败时回退到原有数字接口"""
    global kg_api_instance
    songs = []
    if COPY_SONG_AVAILABLE and kg_api_instance:
        try:
            # 假设方法名 get_playlist_detail 或 get_song_list
            detail = kg_api_instance.get_playlist_detail(pid)
            for item in detail:
                songs.append({
                    'songname': item.get('songname', item.get('name', '')),
                    'singername': item.get('singername', item.get('singer', '')),
                    'hash': item.get('hash', ''),
                    'sqhash': item.get('sqhash', ''),
                    '320hash': item.get('320hash', ''),
                    'album_id': item.get('album_id', 0)
                })
            log.info(f'✅ copyKgSong 获取到 {len(songs)} 首歌曲')
            return songs
        except Exception as e:
            log.warning(f'copyKgSong 获取歌曲失败，尝试备用方式: {e}')

    # 备用：数字ID接口（或页面解析，视需要可加）
    log.error(f'备用获取歌曲未实现，歌单 {pid} 无法同步')
    return songs

def get_play_url(song):
    """下载链接：优先 copyKgSong，否则 gateway"""
    global kg_api_instance
    if COPY_SONG_AVAILABLE and kg_api_instance:
        try:
            # 假设 copyKgSong 提供 get_song_url 方法
            url = kg_api_instance.get_song_url(song.get('hash'))
            if url: return url
        except: pass

    # 回退到我们稳定的 gateway 方法
    mid = '239526275778893399526700786998289824956'
    userid = '0'
    for h in [song.get('sqhash'), song.get('320hash'), song.get('hash')]:
        if not h: continue
        raw = h + '57ae12eb6890223e355ccfcb74edf70d1005' + mid + userid
        key = hashlib.md5(raw.encode()).hexdigest()
        url = f'https://gateway.kugou.com/i/v2/?dfid=&pid=2&mid={mid}&cmd=26&token=&hash={h}&area_code=1&behavior=play&appid=1005&module=&vipType=6&ptype=1&userid={userid}&mtype=1&album_id={song.get("album_id",0)}&pidversion=3001&key={key}&version=10209&album_audio_id=&with_res_tag=1'
        try:
            r = requests.get(url, headers={
                'Host': 'gateway.kugou.com', 'x-router': 'tracker.kugou.com',
                'User-Agent': 'Android511-AndroidPhone-10209-14-0-NetMusic-wifi'}, timeout=10)
            text = r.text.replace('<!--KG_TAG_RES_START-->', '').replace('<!--KG_TAG_RES_END-->', '')
            data = json.loads(text)
            if data.get('url'): return data['url'][0]
        except: pass
    return None

def download_file(url, path):
    try:
        r = requests.get(url, stream=True, timeout=60)
        r.raise_for_status()
        with open(path, 'wb') as f:
            for chunk in r.iter_content(8192): f.write(chunk)
        log.info(f'✅ 已下载：{path.name}')
    except Exception as e:
        log.error(f'❌ 下载失败 {path.name}: {e}')

def safe_name(s): return re.sub(r'[\\/*?:"<>|]', '_', str(s))

def sync_playlist(pid, name):
    songs = get_songs(pid)
    if not songs:
        log.error(f'歌单 {name} 无歌曲，跳过')
        return
    folder = Path(DOWNLOAD_DIR) / safe_name(name)
    folder.mkdir(parents=True, exist_ok=True)
    for song in songs:
        fname = safe_name(f'{song["singername"]} - {song["songname"]}.mp3')
        path = folder / fname
        if path.exists(): continue
        url = get_play_url(song)
        if url: download_file(url, path)
        else: log.info(f'🔇 无法获取链接：{fname}')

def sync_all():
    pls = load_playlists()
    if not pls: return
    log.info('⏰ 定时同步开始')
    for p in pls:
        sync_playlist(p['id'], p.get('name', '歌单'))
    log.info('✅ 定时同步完成')

def run_scheduler():
    if INTERVAL_MIN > 0:
        schedule.every(INTERVAL_MIN).minutes.do(sync_all)
        log.info(f'📅 定时同步间隔 {INTERVAL_MIN} 分钟')
        while True: schedule.run_pending(); time.sleep(30)

# ---------- Web 路由 ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/logs', methods=['GET'])
def api_logs():
    try:
        with open(LOG_FILE, 'r') as f: return jsonify([l.rstrip('\n') for l in f.readlines()[-200:]])
    except: return jsonify([])

@app.route('/api/sync', methods=['POST'])
def api_sync():
    data = request.get_json()
    if data.get('all'):
        threading.Thread(target=sync_all, daemon=True).start()
        return jsonify({'message': '全量同步开始'})
    ids = data.get('ids', [])
    pls = load_playlists()
    for p in pls:
        if p['id'] in ids:
            threading.Thread(target=sync_playlist, args=(p['id'], p.get('name', '歌单')), daemon=True).start()
    return jsonify({'message': f'开始同步 {len(ids)} 个歌单'})

# ---------- 启动 ----------
if __name__ == '__main__':
    log.info('🚀 扫码版酷狗同步服务启动')
    threading.Thread(target=run_scheduler, daemon=True).start()
    log.info(f'🌐 面板监听 0.0.0.0:{WEB_PORT}')
    app.run(host='0.0.0.0', port=WEB_PORT, debug=False)
