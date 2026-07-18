#!/usr/bin/env python3
"""
酷狗歌单同步服务 + Web 控制面板（增强解析版）
- 支持粘贴 gcid 链接、数字ID
- 自动解析歌曲和名称
- 下载基于 musicapi 的 gateway 接口
"""
import os, re, json, time, threading, sys, logging, traceback, hashlib
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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

PLAYLIST_STORE = '/app/data/playlists.json'

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
# 签名函数（用于某些接口）
# ============================================================
def kugou_music_sign(url):
    uri = url.split('?')[1]
    uri_list = uri.split('&')
    ordered_list = sorted(uri_list)
    uri = 'OIlwieks28dk2k092lksi2UIkp' + "".join(ordered_list) + 'OIlwieks28dk2k092lksi2UIkp'
    return hashlib.md5(uri.encode()).hexdigest()

# ============================================================
# 智能解析输入（链接或ID）
# ============================================================
def extract_id_from_input(raw):
    raw = raw.strip()
    # 纯数字
    if re.fullmatch(r'\d+', raw):
        return {'type': 'numeric', 'id': raw}
    # 包含 kugou.com 的链接
    if 'kugou.com' in raw:
        # 尝试提取 gcid
        parsed = urlparse(raw)
        params = parse_qs(parsed.query)
        gcid = params.get('gcid', params.get('src_cid', []))
        if gcid:
            gcid_val = gcid[0]
            if gcid_val.startswith('gcid_'): gcid_val = gcid_val[5:]
            return {'type': 'gcid', 'id': gcid_val}
        # 传统 playlist/数字ID
        match = re.search(r'/playlist/(\d+)\.html', raw)
        if match: return {'type': 'numeric', 'id': match.group(1)}
        # specialid 负数（一般不可用，但记录下来）
        match = re.search(r'specialid=(-?\d+)', raw)
        if match: return {'type': 'numeric', 'id': match.group(1)}
    # 以 gcid_ 开头
    if raw.startswith('gcid_'):
        return {'type': 'gcid', 'id': raw[5:]}
    # 默认作为数字ID
    if re.fullmatch(r'[-]?\d+', raw):
        return {'type': 'numeric', 'id': raw}
    # 其他，尝试作为 gcid
    return {'type': 'gcid', 'id': raw}

# ============================================================
# 获取歌单歌曲（核心）
# ============================================================
def get_songs(pid, id_type='numeric'):
    if id_type == 'numeric':
        return get_songs_numeric(pid)
    else:
        return get_songs_gcid(pid)

# -- 数字ID接口（gatewayretry）--
def get_songs_numeric(pid):
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
                name = item.get('name', '')
                if ' - ' in name:
                    singer, songname = name.split(' - ', 1)
                else:
                    singer, songname = '', name
                songs.append({
                    'songname': songname.strip(),
                    'singername': singer.strip(),
                    'hash': item.get('hash', ''),
                    'sqhash': item.get('sqhash', ''),
                    '320hash': item.get('320hash', ''),
                    'album_id': item.get('album_id', 0)
                })
            return songs
        else:
            log_error(f'数字ID接口返回异常: {data}')
    except Exception as e:
        log_error(f'数字ID接口请求失败: {e}')
    return []

# -- gcid 专用解析 --
def get_songs_gcid(gcid):
    # 方法1：直接请求移动端 gcid 页面，提取 __NUXT__
    songs = _parse_mobile_page(gcid)
    if songs: return songs
    # 方法2：尝试数字接口（可能某些 gcid 对应 specialid？）
    # 部分 gcid 可通过特定接口转换为数字，但未公开，先跳过
    log_error(f'gcid {gcid} 无法获取歌曲，已尝试页面解析')
    return []

def _parse_mobile_page(gcid):
    url = f'https://m.kugou.com/songlist/gcid_{gcid}/'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    }
    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        if r.status_code != 200:
            log_error(f'移动端页面返回 {r.status_code}')
            return []
        html = r.text
        # 尝试 __NUXT__
        match = re.search(r'window\.__NUXT__\s*=\s*(\{.*?\});\s*</script>', html, re.DOTALL)
        if not match:
            match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>', html, re.DOTALL)
        if not match:
            # 尝试提取 JSON 块
            match = re.search(r'<script>window\.__NUXT__\s*=\s*(\{.*?\});</script>', html, re.DOTALL)
        if match:
            json_str = match.group(1).replace('undefined', 'null').replace("'", '"')
            try:
                data = json.loads(json_str)
            except:
                # 如果不是标准 JSON，尝试用正则提取歌曲列表
                return _extract_songs_from_html(html)
            # 遍历可能的键
            def extract_songs_from_obj(obj):
                if isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, dict) and ('songname' in item or 'name' in item or 'hash' in item):
                            return obj
                        res = extract_songs_from_obj(item)
                        if res: return res
                elif isinstance(obj, dict):
                    for k in ('songlist', 'playlist', 'data', 'info', 'list', 'songs', 'songList'):
                        if k in obj and isinstance(obj[k], list):
                            return obj[k]
                    for v in obj.values():
                        res = extract_songs_from_obj(v)
                        if res: return res
                return None
            songs_data = extract_songs_from_obj(data)
            if songs_data:
                songs = []
                for item in songs_data:
                    name = item.get('songname') or item.get('name', '')
                    singer = item.get('singername') or item.get('singer', '')
                    if ' - ' in name:
                        parts = name.split(' - ', 1)
                        singer = parts[0]
                        songname = parts[1]
                    else:
                        songname = name
                    songs.append({
                        'songname': songname.strip(),
                        'singername': singer.strip() if singer else '',
                        'hash': item.get('hash', ''),
                        'sqhash': item.get('sqhash', ''),
                        '320hash': item.get('320hash', ''),
                        'album_id': item.get('album_id', 0)
                    })
                return songs
        # 没有找到 __NUXT__，尝试从 HTML 中直接提取
        return _extract_songs_from_html(html)
    except Exception as e:
        log_error(f'页面解析异常: {e}')
        return []

def _extract_songs_from_html(html):
    # 暴力正则提取所有可能的 hash 相关字段
    # 但成功率低，这里仅作为最后手段
    log_error('⚠️ 无法解析页面结构，请尝试分享出数字ID再添加')
    return []

# ============================================================
# 获取歌单名称
# ============================================================
def get_playlist_name(pid, id_type):
    if id_type == 'gcid':
        url = f'https://m.kugou.com/songlist/gcid_{pid}/'
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            r = requests.get(url, headers=headers, timeout=10)
            title_match = re.search(r'<title>(.*?)</title>', r.text)
            if title_match:
                return title_match.group(1).replace(' - 酷狗音乐', '').strip()
        except:
            pass
        return pid
    else:
        try:
            r = requests.get(f'https://www.kugou.com/yy/playlist/{pid}.html', timeout=10)
            title_match = re.search(r'<title>(.*?)</title>', r.text)
            if title_match:
                return title_match.group(1).replace(' - 酷狗音乐', '').strip()
        except:
            pass
        return pid

# ============================================================
# 获取下载链接（gateway，无需 Cookie）
# ============================================================
def get_play_url(song):
    mid = '239526275778893399526700786998289824956'
    userid = '0'
    hashes = [song.get('sqhash'), song.get('320hash'), song.get('hash')]
    for h in hashes:
        if not h: continue
        raw = h + '57ae12eb6890223e355ccfcb74edf70d1005' + mid + userid
        str_md5 = hashlib.md5(raw.encode()).hexdigest()
        url = (f'https://gateway.kugou.com/i/v2/'
               f'?dfid=&pid=2&mid={mid}&cmd=26&token=&hash={h}&area_code=1&behavior=play'
               f'&appid=1005&module=&vipType=6&ptype=1&userid={userid}&mtype=1'
               f'&album_id={song.get("album_id", 0)}&pidversion=3001&key={str_md5}'
               f'&version=10209&album_audio_id=&with_res_tag=1')
        headers = {
            'Host': 'gateway.kugou.com',
            'x-router': 'tracker.kugou.com',
            'User-Agent': 'Android511-AndroidPhone-10209-14-0-NetMusic-wifi'
        }
        try:
            r = requests.get(url, headers=headers, timeout=10)
            text = r.text.replace('<!--KG_TAG_RES_START-->', '').replace('<!--KG_TAG_RES_END-->', '')
            data = json.loads(text)
            if data.get('url'):
                return data['url'][0]
        except Exception as e:
            log_error(f'获取播放链接失败 hash={h}: {e}')
    return None

def download_file(url, filepath):
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
        if filepath.exists(): filepath.unlink()
        return False

def safe_name(text):
    return re.sub(r'[\\/*?:"<>|]', '_', str(text))

# ============================================================
# 同步逻辑
# ============================================================
def sync_playlist(pid, name, id_type='numeric'):
    folder = Path(DOWNLOAD_DIR) / safe_name(name)
    folder.mkdir(parents=True, exist_ok=True)
    songs = get_songs(pid, id_type)
    if not songs:
        log_error(f'❌ 歌单 {name} ({pid}) 获取歌曲失败')
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
        log_info('⚠️ 没有待同步的歌单')
        return
    log_info('⏰ 定时同步开始')
    for pl in playlists:
        pid = pl.get('id')
        name = pl.get('name', '未知歌单')
        id_type = pl.get('type', 'numeric')
        log_info(f'🎵 同步歌单：{name} ({pid})')
        sync_playlist(pid, name, id_type)
    log_info('✅ 定时同步完成')

def run_scheduler():
    if INTERVAL_MIN > 0:
        schedule.every(INTERVAL_MIN).minutes.do(sync_all)
        log_info(f'📅 定时同步间隔 {INTERVAL_MIN} 分钟')
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
    raw = data.get('input', '').strip()
    if not raw: return jsonify({'error': '请输入内容'}), 400
    info = extract_id_from_input(raw)
    pid = info['id']
    id_type = info['type']
    name = get_playlist_name(pid, id_type)
    playlists = load_playlists()
    if any(p['id'] == pid for p in playlists):
        return jsonify({'error': '歌单已存在'}), 409
    playlists.append({'id': pid, 'type': id_type, 'name': name})
    save_playlists(playlists)
    log_info(f'➕ 添加歌单：{name} ({pid}) type={id_type}')
    # 立即同步
    t = threading.Thread(target=sync_playlist, args=(pid, name, id_type), daemon=True)
    t.start()
    return jsonify({'message': '添加成功，已开始同步'})

@app.route('/api/playlists/<pid>', methods=['DELETE'])
def api_delete_playlist(pid):
    playlists = load_playlists()
    new_list = [p for p in playlists if p['id'] != pid]
    if len(new_list) == len(playlists): return jsonify({'error': '不存在'}), 404
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
        return jsonify({'message': '全量同步开始'})
    ids = data.get('ids')
    if not isinstance(ids, list) or len(ids) == 0:
        return jsonify({'error': 'ids 必须是非空数组'}), 400
    playlists = load_playlists()
    for pl in playlists:
        if pl['id'] in ids:
            t = threading.Thread(target=sync_playlist, args=(pl['id'], pl['name'], pl.get('type', 'numeric')), daemon=True)
            t.start()
    return jsonify({'message': f'已开始同步 {len(ids)} 个歌单'})

# 新增：手动调试接口（可查看页面响应）
@app.route('/api/debug', methods=['POST'])
def api_debug():
    data = request.get_json()
    raw = data.get('input', '')
    info = extract_id_from_input(raw)
    if info['type'] == 'gcid':
        url = f'https://m.kugou.com/songlist/gcid_{info["id"]}/'
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            r = requests.get(url, headers=headers, timeout=10)
            return jsonify({'status': r.status_code, 'text_preview': r.text[:2000]})
        except Exception as e:
            return jsonify({'error': str(e)})
    return jsonify({'error': '仅支持 gcid 类型调试'})

# ============================================================
# 启动
# ============================================================
if __name__ == '__main__':
    try:
        log_info('🚀 酷狗同步服务启动')
        threading.Thread(target=run_scheduler, daemon=True).start()
        log_info(f'🌐 面板监听 http://0.0.0.0:{WEB_PORT}')
        app.run(host='0.0.0.0', port=WEB_PORT, debug=False, use_reloader=False)
    except Exception as e:
        log_error(f'💥 启动失败: {traceback.format_exc()}')
        while True: time.sleep(60)
