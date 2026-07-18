#!/usr/bin/env python3
"""
酷狗歌单同步服务（已验证版）
- 支持粘贴 m.kugou.com/songlist/gcid_xx 链接
- 自动解析页面，提取歌曲列表和歌单名称
- 下载使用 gateway.kugou.com 稳定接口（无需 Cookie）
"""
import os, re, json, time, threading, sys, logging, traceback, hashlib
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import requests
from flask import Flask, render_template, request, jsonify
import schedule

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

DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', '/music')
WEB_PORT    = int(os.getenv('WEB_PORT', '5000'))
INTERVAL_MIN = int(os.getenv('INTERVAL_MIN', '60'))

app = Flask(__name__)
PLAYLIST_STORE = '/app/data/playlists.json'

def load_playlists():
    try:
        with open(PLAYLIST_STORE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return []
def save_playlists(plist):
    os.makedirs(os.path.dirname(PLAYLIST_STORE), exist_ok=True)
    with open(PLAYLIST_STORE, 'w', encoding='utf-8') as f: json.dump(plist, f, ensure_ascii=False, indent=2)

# ========== 智能解析输入 ==========
def extract_info(raw):
    raw = raw.strip()
    if re.fullmatch(r'\d+', raw): return 'numeric', raw
    if 'kugou.com' in raw:
        parsed = urlparse(raw)
        params = parse_qs(parsed.query)
        gcid = params.get('gcid', params.get('src_cid', []))
        if gcid:
            val = gcid[0]
            if val.startswith('gcid_'): val = val[5:]
            return 'gcid', val
        match = re.search(r'/playlist/(\d+)\.html', raw)
        if match: return 'numeric', match.group(1)
        match = re.search(r'specialid=(-?\d+)', raw)
        if match: return 'numeric', match.group(1)
    if raw.startswith('gcid_'): return 'gcid', raw[5:]
    return 'numeric', raw

# ========== 获取歌单名称与歌曲（核心） ==========
def get_playlist_info(pid, id_type):
    if id_type == 'gcid':
        return _parse_gcid_page(pid)
    else:
        # 数字ID：尝试 gatewayretry 接口
        songs = _get_songs_numeric(pid)
        name = _get_name_numeric(pid)
        return name, songs

def _get_songs_numeric(pid):
    url = f'http://gatewayretry.kugou.com/v2/get_other_list_file?specialid={pid}&need_sort=1&module=CloudMusic&clientver=11239&pagesize=300&specalidpgc={pid}&userid=0&page=1&type=0&area_code=1&appid=1005'
    headers = {
        'User-Agent': 'Android9-AndroidPhone-11239-18-0-playlist-wifi',
        'Host': 'gatewayretry.kugou.com',
        'x-router': 'pubsongscdn.kugou.com',
        'mid': '239526275778893399526700786998289824956',
        'dfid': '-',
        'clienttime': str(int(time.time()))
    }
    signature = _sign(url)
    try:
        r = requests.get(url + '&signature=' + signature, headers=headers, timeout=15)
        data = r.json()
        if data.get('status') == 1 and data.get('data'):
            songs = []
            for item in data['data']['info']:
                name = item['name']
                if ' - ' in name:
                    singer, songname = name.split(' - ', 1)
                else:
                    singer, songname = '', name
                songs.append({'songname': songname.strip(), 'singername': singer.strip(), 'hash': item.get('hash',''), 'sqhash': item.get('sqhash',''), '320hash': item.get('320hash',''), 'album_id': item.get('album_id',0)})
            return songs
    except Exception as e: log_error(f'数字接口失败: {e}')
    return []

def _get_name_numeric(pid):
    try:
        r = requests.get(f'https://www.kugou.com/yy/playlist/{pid}.html', timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        match = re.search(r'<title>(.*?)</title>', r.text)
        if match: return match.group(1).replace(' - 酷狗音乐', '').strip()
    except: pass
    return pid

def _parse_gcid_page(gcid):
    url = f'https://m.kugou.com/songlist/gcid_{gcid}/'
    headers = {'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'}
    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        if r.status_code != 200: return None, []
        html = r.text
        # 提取 __NUXT__ 数据
        match = re.search(r'window\.__NUXT__\s*=\s*(\{.*?\});\s*</script>', html, re.DOTALL)
        if not match:
            match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>', html, re.DOTALL)
        if match:
            json_str = match.group(1).replace('undefined', 'null')
            try: data = json.loads(json_str)
            except: return None, []
            songs = _extract_songs_from_nuxt(data)
            title_match = re.search(r'<title>(.*?)</title>', html)
            name = title_match.group(1).replace(' - 酷狗音乐', '').strip() if title_match else gcid
            return name, songs
        return None, []
    except Exception as e: log_error(f'gcid页面解析失败: {e}'); return None, []

def _extract_songs_from_nuxt(data):
    # 遍历可能的路径
    def find_songs(obj):
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict) and ('hash' in item or 'songname' in item):
                    return obj
                res = find_songs(item)
                if res: return res
        elif isinstance(obj, dict):
            for key in ('songlist', 'playlist', 'list', 'info', 'songs', 'data'):
                if key in obj and isinstance(obj[key], list):
                    return obj[key]
            for v in obj.values():
                res = find_songs(v)
                if res: return res
        return None
    raw_songs = find_songs(data)
    songs = []
    if raw_songs:
        for s in raw_songs:
            name = s.get('songname') or s.get('name', '')
            singer = s.get('singername') or s.get('singer', '')
            if ' - ' in name:
                parts = name.split(' - ', 1)
                singer, name = parts[0], parts[1]
            songs.append({'songname': name.strip(), 'singername': singer.strip(), 'hash': s.get('hash',''), 'sqhash': s.get('sqhash',''), '320hash': s.get('320hash',''), 'album_id': s.get('album_id',0)})
    return songs

def _sign(url):
    uri = url.split('?')[1]
    ordered = sorted(uri.split('&'))
    uri = 'OIlwieks28dk2k092lksi2UIkp' + "".join(ordered) + 'OIlwieks28dk2k092lksi2UIkp'
    return hashlib.md5(uri.encode()).hexdigest()

# ========== 下载（gateway，零 Cookie） ==========
def get_play_url(song):
    mid = '239526275778893399526700786998289824956'
    userid = '0'
    for h in [song.get('sqhash'), song.get('320hash'), song.get('hash')]:
        if not h: continue
        raw = h + '57ae12eb6890223e355ccfcb74edf70d1005' + mid + userid
        key = hashlib.md5(raw.encode()).hexdigest()
        url = f'https://gateway.kugou.com/i/v2/?dfid=&pid=2&mid={mid}&cmd=26&token=&hash={h}&area_code=1&behavior=play&appid=1005&module=&vipType=6&ptype=1&userid={userid}&mtype=1&album_id={song.get("album_id",0)}&pidversion=3001&key={key}&version=10209&album_audio_id=&with_res_tag=1'
        try:
            r = requests.get(url, headers={'Host': 'gateway.kugou.com','x-router': 'tracker.kugou.com','User-Agent': 'Android511-AndroidPhone-10209-14-0-NetMusic-wifi'}, timeout=10)
            text = r.text.replace('<!--KG_TAG_RES_START-->','').replace('<!--KG_TAG_RES_END-->','')
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
        log_info(f'✅ 已下载：{path.name}')
        return True
    except Exception as e:
        log_error(f'❌ 下载失败 {path.name}: {e}')
        if path.exists(): path.unlink()
        return False

def safe_name(s): return re.sub(r'[\\/*?:"<>|]', '_', str(s))

# ========== 同步逻辑 ==========
def sync_playlist(pid, name, id_type):
    _, songs = get_playlist_info(pid, id_type)
    if not songs:
        log_error(f'❌ 歌单 {name} 无歌曲')
        return
    folder = Path(DOWNLOAD_DIR) / safe_name(name)
    folder.mkdir(parents=True, exist_ok=True)
    for song in songs:
        fname = safe_name(f'{song["singername"]} - {song["songname"]}.mp3')
        path = folder / fname
        if path.exists(): log_info(f'⏭️ 跳过：{fname}'); continue
        url = get_play_url(song)
        if url: download_file(url, path)
        else: log_info(f'🔇 无法获取链接：{fname}')

def sync_all():
    pls = load_playlists()
    if not pls: return
    log_info('⏰ 定时同步开始')
    for p in pls: sync_playlist(p['id'], p.get('name','未知'), p.get('type','numeric'))
    log_info('✅ 定时同步完成')

def scheduler():
    if INTERVAL_MIN > 0:
        schedule.every(INTERVAL_MIN).minutes.do(sync_all)
        log_info(f'📅 定时同步间隔 {INTERVAL_MIN} 分钟')
        while True: schedule.run_pending(); time.sleep(30)

# ========== Web 路由 ==========
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/playlists', methods=['GET'])
def api_playlists(): return jsonify(load_playlists())

@app.route('/api/playlists', methods=['POST'])
def api_add():
    data = request.get_json()
    raw = data.get('input', '').strip()
    if not raw: return jsonify({'error': '请输入链接或ID'}), 400
    id_type, pid = extract_info(raw)
    name, songs = get_playlist_info(pid, id_type)
    if not name: name = pid
    pls = load_playlists()
    if any(p['id'] == pid for p in pls): return jsonify({'error': '歌单已存在'}), 409
    pls.append({'id': pid, 'type': id_type, 'name': name})
    save_playlists(pls)
    log_info(f'➕ 添加歌单：{name} ({pid})')
    threading.Thread(target=sync_playlist, args=(pid, name, id_type), daemon=True).start()
    return jsonify({'message': '添加成功，已开始同步'})

@app.route('/api/playlists/<pid>', methods=['DELETE'])
def api_del(pid):
    pls = load_playlists()
    new = [p for p in pls if p['id'] != pid]
    if len(new) == len(pls): return jsonify({'error': '不存在'}), 404
    save_playlists(new)
    log_info(f'🗑️ 删除歌单：{pid}')
    return jsonify({'message': '删除成功'})

@app.route('/api/logs', methods=['GET'])
def api_logs():
    try:
        with open(LOG_FILE, 'r') as f:
            lines = f.readlines()[-200:]
            return jsonify([l.rstrip('\n') for l in lines])
    except: return jsonify([])

@app.route('/api/sync', methods=['POST'])
def api_sync():
    data = request.get_json()
    if data.get('all'):
        threading.Thread(target=sync_all, daemon=True).start()
        return jsonify({'message': '全量同步开始'})
    ids = data.get('ids')
    if not ids: return jsonify({'error': 'ids required'}), 400
    pls = load_playlists()
    for p in pls:
        if p['id'] in ids:
            threading.Thread(target=sync_playlist, args=(p['id'], p.get('name',''), p.get('type','numeric')), daemon=True).start()
    return jsonify({'message': f'开始同步 {len(ids)} 个歌单'})

if __name__ == '__main__':
    try:
        log_info('🚀 服务启动')
        threading.Thread(target=scheduler, daemon=True).start()
        log_info(f'🌐 面板监听 0.0.0.0:{WEB_PORT}')
        app.run(host='0.0.0.0', port=WEB_PORT, debug=False)
    except Exception as e:
        log_error(f'💥 启动失败: {traceback.format_exc()}')
        while True: time.sleep(60)
