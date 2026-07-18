#!/usr/bin/env python3
"""
酷狗歌单同步服务 + Web 控制面板（全自动识别版）
- 支持粘贴酷狗分享链接、gcid、数字ID
- 自动从酷狗页面获取歌单名称和歌曲列表
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

# 歌单存储
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
# musicapi 签名
# ============================================================
def kugou_music_sign(url):
    uri = url.split('?')[1]
    uri_list = uri.split('&')
    ordered_list = sorted(uri_list)
    uri = 'OIlwieks28dk2k092lksi2UIkp' + "".join(ordered_list) + 'OIlwieks28dk2k092lksi2UIkp'
    return hashlib.md5(uri.encode()).hexdigest()

# ============================================================
# 解析分享链接，提取 gcid 或数字ID
# ============================================================
def extract_id_from_url(url_or_id):
    """ 从链接或ID字符串中提取可用的标识，返回 (id_type, id_value, possible_name) """
    url_or_id = url_or_id.strip()
    # 如果是纯数字 → 数字playlistid
    if re.fullmatch(r'\d+', url_or_id):
        return 'numeric', url_or_id, '歌单'
    # 如果是完整链接（包含 m.kugou.com 或 t.kugou.com 等）
    if 'kugou.com' in url_or_id:
        parsed = urlparse(url_or_id)
        params = parse_qs(parsed.query)
        # 提取 gcid（如 gcid_3z10c6tqvz6z02a）
        gcid = params.get('gcid', params.get('src_cid', []))
        if gcid:
            gcid_val = gcid[0]
            if gcid_val.startswith('gcid_'):
                gcid_val = gcid_val[5:]   # 去掉 "gcid_" 前缀
            return 'gcid', gcid_val, '歌单'
        # 传统 playlist 数字ID（如 https://www.kugou.com/yy/playlist/123456.html）
        match = re.search(r'/playlist/(\d+)\.html', url_or_id)
        if match:
            return 'numeric', match.group(1), '歌单'
        # 如果是 activity 页面，尝试提取 specialid（负数）
        match = re.search(r'specialid=(-?\d+)', url_or_id)
        if match:
            return 'numeric', match.group(1), '歌单'
    # 如果包含 "gcid_" 前缀，直接作为 gcid
    if url_or_id.startswith('gcid_'):
        return 'gcid', url_or_id[5:], '歌单'
    # 其他情况，猜测为普通ID
    return 'unknown', url_or_id, '歌单'

# ============================================================
# 获取歌单歌曲（根据类型分发）
# ============================================================
def get_songs(pid, id_type='numeric'):
    if id_type == 'numeric':
        return get_songs_numeric(pid)
    else:
        return get_songs_by_page(pid)

# 数字ID接口（gatewayretry）
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
            log_error(f'数字ID获取失败 pid={pid}: {data}')
    except Exception as e:
        log_error(f'数字ID获取异常 pid={pid}: {e}')
    return []

# 页面解析（支持gcid等）
def get_songs_by_page(gcid):
    # 尝试移动端页面
    url = f'https://m.kugou.com/songlist/gcid_{gcid}/'
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15'
    }
    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        # 提取 window.__INITIAL_STATE__ 或 __NUXT__
        text = r.text
        # 尝试 __NUXT__
        match = re.search(r'window\.__NUXT__\s*=\s*(\{.*?\});\s*</script>', text, re.DOTALL)
        if not match:
            match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>', text, re.DOTALL)
        if match:
            json_str = match.group(1)
            # 修复某些未转义的特殊字符
            json_str = json_str.replace('undefined', 'null')
            data = json.loads(json_str)
            # 提取歌曲列表，结构因页面而异
            songs_data = []
            if 'songlist' in data and 'list' in data['songlist']:
                songs_data = data['songlist']['list']
            elif 'playlist' in data and 'info' in data['playlist']:
                songs_data = data['playlist']['info']
            elif 'data' in data and 'list' in data['data']:
                songs_data = data['data']['list']
            if songs_data:
                songs = []
                for item in songs_data:
                    # 兼容不同字段名
                    name = item.get('songname') or item.get('name', '')
                    singer = item.get('singername') or item.get('singer', '')
                    if ' - ' in name:
                        parts = name.split(' - ', 1)
                        singer = parts[0]
                        songname = parts[1]
                    else:
                        songname = name
                        singer = singer if singer else ''
                    songs.append({
                        'songname': songname,
                        'singername': singer,
                        'hash': item.get('hash', ''),
                        'sqhash': item.get('sqhash', ''),
                        '320hash': item.get('320hash', ''),
                        'album_id': item.get('album_id', 0)
                    })
                return songs
    except Exception as e:
        log_error(f'页面解析失败 gcid={gcid}: {e}')
    return []

# ============================================================
# 获取歌单名称（通过页面解析或接口）
# ============================================================
def get_playlist_name(pid, id_type):
    # 名称从页面解析时一并获取，如果已存在则直接用，这里仅提供简单逻辑
    # 实际上在添加歌单时我们会尝试解析一次来获取名称
    return pid  # 临时返回ID，实际解析时会在添加流程中获取

# ============================================================
# 获取下载链接（gateway，无需 Cookie）
# ============================================================
def get_play_url(song):
    mid = '239526275778893399526700786998289824956'
    userid = '0'
    hashes = [song.get('sqhash'), song.get('320hash'), song.get('hash')]
    for h in hashes:
        if not h:
            continue
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
        if filepath.exists():
            filepath.unlink()
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
        log_info('⚠️ 没有待同步的歌单，请先添加')
        return
    log_info('⏰ 定时同步开始')
    for pl in playlists:
        pid = pl.get('id')
        name = pl.get('name', '未知歌单')
        id_type = pl.get('type', 'numeric')
        log_info(f'🎵 同步歌单：{name} ({pid})')
        try:
            sync_playlist(pid, name, id_type)
        except Exception as e:
            log_error(f'同步歌单 {name} 出错: {e}')
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
    if not raw:
        return jsonify({'error': '请输入歌单 ID 或分享链接'}), 400
    # 解析输入
    id_type, real_id, _ = extract_id_from_url(raw)
    if id_type == 'unknown':
        # 尝试作为数字处理
        if re.fullmatch(r'\d+', raw):
            id_type, real_id = 'numeric', raw
        else:
            id_type, real_id = 'gcid', raw
    # 尝试获取歌单名称和歌曲列表
    name = None
    songs = get_songs(real_id, id_type)
    if id_type == 'gcid':
        # 页面解析可能已经包含名称，我们再单独请求一次页面提取名称
        try:
            url = f'https://m.kugou.com/songlist/gcid_{real_id}/'
            headers = {'User-Agent': 'Mozilla/5.0'}
            r = requests.get(url, headers=headers, timeout=10)
            title_match = re.search(r'<title>(.*?)</title>', r.text)
            if title_match:
                name = title_match.group(1).replace(' - 酷狗音乐', '').strip()
        except:
            pass
    if not name:
        # 对于数字ID，也可以尝试网页版
        if id_type == 'numeric':
            try:
                url = f'https://www.kugou.com/yy/playlist/{real_id}.html'
                headers = {'User-Agent': 'Mozilla/5.0'}
                r = requests.get(url, headers=headers, timeout=10)
                title_match = re.search(r'<title>(.*?)</title>', r.text)
                if title_match:
                    name = title_match.group(1).replace(' - 酷狗音乐', '').strip()
            except:
                pass
    if not name:
        name = raw if len(raw) < 20 else (real_id if real_id else raw)

    # 保存
    playlists = load_playlists()
    if any(p['id'] == real_id for p in playlists):
        return jsonify({'error': '歌单已存在'}), 409
    playlists.append({'id': real_id, 'type': id_type, 'name': name or real_id})
    save_playlists(playlists)
    log_info(f'➕ 添加歌单：{name} ({real_id})')
    # 立即后台同步一次
    t = threading.Thread(target=sync_playlist, args=(real_id, name, id_type), daemon=True)
    t.start()
    return jsonify({'message': f'添加成功，已开始同步', 'playlist': {'id': real_id, 'name': name, 'type': id_type}})

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
    for pl in playlists:
        if pl['id'] in ids:
            t = threading.Thread(target=sync_playlist, args=(pl['id'], pl['name'], pl.get('type', 'numeric')), daemon=True)
            t.start()
    return jsonify({'message': f'已开始同步 {len(ids)} 个歌单'})

# ============================================================
# 启动
# ============================================================
if __name__ == '__main__':
    try:
        log_info('🚀 酷狗同步服务启动（全自动识别）')
        threading.Thread(target=run_scheduler, daemon=True).start()
        log_info(f'🌐 面板监听 http://0.0.0.0:{WEB_PORT}')
        app.run(host='0.0.0.0', port=WEB_PORT, debug=False, use_reloader=False)
    except Exception as e:
        log_error(f'💥 启动失败: {traceback.format_exc()}')
        with open(ERROR_LOG_FILE, 'a') as f:
            traceback.print_exc(file=f)
        while True:
            time.sleep(60)
