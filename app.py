#!/usr/bin/env python3
"""
酷狗歌单同步服务（最终稳定版）
- 导入：使用 Cookie + 签名接口获取用户所有歌单（返回正数 playlistid）
- 下载：基于 musicapi 的 gateway 接口（无需 Cookie）
"""
import os, re, json, time, threading, sys, logging, traceback, hashlib
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote
import requests
from flask import Flask, render_template, request, jsonify
import schedule

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

def load_playlists():
    try:
        with open(PLAYLIST_STORE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except: return []

def save_playlists(plist):
    os.makedirs(os.path.dirname(PLAYLIST_STORE), exist_ok=True)
    with open(PLAYLIST_STORE, 'w', encoding='utf-8') as f:
        json.dump(plist, f, ensure_ascii=False, indent=2)

# ---------- 通用签名函数 ----------
def kg_sign(params_dict):
    """根据酷狗移动端 API 规则生成签名"""
    ordered = sorted(params_dict.items())
    uri = "&".join([f"{k}={v}" for k, v in ordered])
    raw = 'OIlwieks28dk2k092lksi2UIkp' + uri + 'OIlwieks28dk2k092lksi2UIkp'
    return hashlib.md5(raw.encode()).hexdigest()

# ---------- 导入用户歌单（核心） ----------
def import_user_playlists(cookie_str):
    """使用 Cookie 拉取用户所有歌单，返回 list of {id, name}"""
    # 解析 Cookie
    cookies = {}
    for item in cookie_str.split(';'):
        item = item.strip()
        if '=' in item:
            k, v = item.split('=', 1)
            cookies[k.strip()] = v.strip()
    userid = cookies.get('KugooID', cookies.get('uid', ''))
    token = cookies.get('t', '')
    mid = cookies.get('kg_mid', cookies.get('mid', '239526275778893399526700786998289824956'))
    if not userid:
        log.error('Cookie 中未找到 KugooID，无法导入')
        return []

    # ------ 方法1：移动端 playlist/list 接口（加签名） ------
    try:
        base_url = 'https://mobilecdn.kugou.com/api/v3/playlist/list'
        params = {
            'format': 'json',
            'userid': userid,
            'page': 1,
            'pagesize': 500,
            'plat': '0',
            'version': '9108',
            'with_song': '0',
            'mid': mid
        }
        sign = kg_sign(params)
        url = f"{base_url}?{'&'.join([f'{k}={quote(str(v))}' for k,v in params.items()])}&signature={sign}"
        headers = {
            'User-Agent': 'Android9-AndroidPhone-11239-18-0-playlist-wifi',
            'Referer': 'https://m.kugou.com',
            'Cookie': cookie_str
        }
        r = requests.get(url, headers=headers, timeout=15, verify=False)
        log.info(f'[导入] 移动端接口 HTTP {r.status_code}')
        if r.status_code == 200:
            try:
                data = r.json()
            except:
                log.error(f'[导入] 移动端接口返回非JSON: {r.text[:200]}')
                data = {}
            if data.get('status') == 1 and data.get('data'):
                plist = data['data'].get('info', data['data'].get('list', []))
                result = [{'id': p['playlistid'], 'name': p.get('title', '歌单')} for p in plist if 'playlistid' in p]
                log.info(f'[导入] 移动端接口成功，获取到 {len(result)} 个歌单')
                return result
            else:
                log.error(f'[导入] 移动端接口返回错误: {data}')
        else:
            log.error(f'[导入] 移动端接口状态码异常')
    except Exception as e:
        log.error(f'[导入] 移动端接口异常: {e}')

    # ------ 方法2：PC 网页端接口（需要 token） ------
    if token:
        try:
            url = 'https://m.kugou.com/yy/playlist/getUserPlaylist'
            params = {
                'userid': userid,
                'token': token,
                'page': 1,
                'pagesize': 500
            }
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Cookie': cookie_str,
                'Referer': 'https://m.kugou.com'
            }
            r = requests.get(url, params=params, headers=headers, timeout=15, verify=False)
            log.info(f'[导入] PC端接口 HTTP {r.status_code}')
            if r.status_code == 200:
                try:
                    data = r.json()
                except:
                    log.error(f'[导入] PC端接口返回非JSON: {r.text[:200]}')
                    data = {}
                if data.get('status') == 1 and data.get('data'):
                    plist = data['data'].get('info', data['data'].get('list', []))
                    result = [{'id': p['playlistid'], 'name': p.get('title', '歌单')} for p in plist if 'playlistid' in p]
                    log.info(f'[导入] PC端接口成功，获取到 {len(result)} 个歌单')
                    return result
                else:
                    log.error(f'[导入] PC端接口返回错误: {data}')
            else:
                log.error(f'[导入] PC端接口状态码异常')
        except Exception as e:
            log.error(f'[导入] PC端接口异常: {e}')
    else:
        log.error('[导入] Cookie 中未找到 token，跳过PC端接口')

    log.error('[导入] 所有接口均失败，请检查 Cookie 是否有效')
    return []

# ---------- 解析输入链接/ID（用于手动添加） ----------
def extract_info(raw):
    raw = raw.strip()
    if re.fullmatch(r'\d+', raw):
        return 'numeric', raw
    if 'kugou.com' in raw:
        parsed = urlparse(raw)
        params = parse_qs(parsed.query)
        gcid = params.get('gcid', params.get('src_cid', []))
        if gcid:
            val = gcid[0].replace('gcid_', '')
            return 'gcid', val
        match = re.search(r'/playlist/(\d+)\.html', raw)
        if match: return 'numeric', match.group(1)
    if raw.startswith('gcid_'):
        return 'gcid', raw[5:]
    return 'numeric', raw

# ---------- 歌曲获取（根据类型） ----------
def get_songs(pid, id_type):
    if id_type == 'gcid':
        return _get_songs_by_gcid(pid)
    else:
        return _get_songs_by_numeric(pid)

def _get_songs_by_numeric(pid):
    url = f'http://gatewayretry.kugou.com/v2/get_other_list_file?specialid={pid}&need_sort=1&module=CloudMusic&clientver=11239&pagesize=300&specalidpgc={pid}&userid=0&page=1&type=0&area_code=1&appid=1005'
    headers = {
        'User-Agent': 'Android9-AndroidPhone-11239-18-0-playlist-wifi',
        'Host': 'gatewayretry.kugou.com',
        'x-router': 'pubsongscdn.kugou.com',
        'mid': '239526275778893399526700786998289824956',
        'dfid': '-',
        'clienttime': str(int(time.time()))
    }
    signature = kg_sign({'specialid': pid, 'need_sort': '1', 'module': 'CloudMusic', 'clientver': '11239',
                         'pagesize': '300', 'specalidpgc': pid, 'userid': '0', 'page': '1', 'type': '0',
                         'area_code': '1', 'appid': '1005'})
    full_url = url + '&signature=' + signature
    try:
        r = requests.get(full_url, headers=headers, timeout=15)
        data = r.json()
        if data.get('status') == 1 and data.get('data'):
            songs = []
            for item in data['data']['info']:
                name = item['name']
                if ' - ' in name:
                    singer, songname = name.split(' - ', 1)
                else:
                    singer, songname = '', name
                songs.append({
                    'songname': songname.strip(), 'singername': singer.strip(),
                    'hash': item.get('hash', ''), 'sqhash': item.get('sqhash', ''),
                    '320hash': item.get('320hash', ''), 'album_id': item.get('album_id', 0)
                })
            log.info(f'✅ 数字ID {pid} 获取到 {len(songs)} 首歌曲')
            return songs
    except Exception as e:
        log.error(f'数字ID接口失败: {e}')
    return []

def _get_songs_by_gcid(gcid):
    """备用的 gcid 接口（可能不稳定，推荐使用导入功能转为数字ID）"""
    url = f'https://m.kugou.com/songlist/getSongList?gcid={gcid}&page=1&pagesize=500'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36',
        'Referer': f'https://m.kugou.com/songlist/gcid_{gcid}/'
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        data = r.json()
        if data.get('status') == 1 and data.get('data'):
            song_list = data['data'].get('info', data['data'].get('list', []))
            songs = []
            for item in song_list:
                name = item.get('songname') or item.get('name', '')
                singer = item.get('singername') or item.get('singer', '')
                if not singer and ' - ' in name:
                    singer, name = name.split(' - ', 1)
                songs.append({
                    'songname': name.strip(), 'singername': singer.strip(),
                    'hash': item.get('hash', ''), 'sqhash': item.get('sqhash', ''),
                    '320hash': item.get('320hash', ''), 'album_id': item.get('album_id', 0)
                })
            log.info(f'✅ gcid {gcid} 获取到 {len(songs)} 首歌曲')
            return songs
        else:
            log.error(f'gcid接口返回异常: {data}')
    except Exception as e:
        log.error(f'gcid接口请求失败: {e}')
    return []

# ---------- 下载链接 ----------
def get_play_url(song):
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
        return True
    except Exception as e:
        log.error(f'❌ 下载失败 {path.name}: {e}')
        if path.exists(): path.unlink()
        return False

def safe_name(s): return re.sub(r'[\\/*?:"<>|]', '_', str(s))

# ---------- 同步 ----------
def sync_playlist(pid, name, id_type='numeric'):
    songs = get_songs(pid, id_type)
    if not songs:
        log.error(f'歌单 {name} 无歌曲')
        return
    folder = Path(DOWNLOAD_DIR) / safe_name(name)
    folder.mkdir(parents=True, exist_ok=True)
    for song in songs:
        fname = safe_name(f'{song["singername"]} - {song["songname"]}.mp3')
        path = folder / fname
        if path.exists():
            log.info(f'⏭️ 跳过：{fname}')
            continue
        url = get_play_url(song)
        if url: download_file(url, path)
        else: log.info(f'🔇 无法获取链接：{fname}')

def sync_all():
    pls = load_playlists()
    if not pls: return
    log.info('⏰ 定时同步开始')
    for p in pls:
        sync_playlist(p['id'], p.get('name', '歌单'), p.get('type', 'numeric'))
    log.info('✅ 定时同步完成')

def run_scheduler():
    if INTERVAL_MIN > 0:
        schedule.every(INTERVAL_MIN).minutes.do(sync_all)
        log.info(f'📅 定时同步间隔 {INTERVAL_MIN} 分钟')
        while True: schedule.run_pending(); time.sleep(30)

# ---------- Web 路由 ----------
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
    # 尝试获取名称
    name = pid
    try:
        if id_type == 'gcid':
            r = requests.get(f'https://m.kugou.com/songlist/gcid_{pid}/', headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            title = re.search(r'<title>(.*?)</title>', r.text)
            if title: name = title.group(1).replace(' - 酷狗音乐', '').strip()
        else:
            r = requests.get(f'https://www.kugou.com/yy/playlist/{pid}.html', headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            title = re.search(r'<title>(.*?)</title>', r.text)
            if title: name = title.group(1).replace(' - 酷狗音乐', '').strip()
    except: pass

    pls = load_playlists()
    if any(p['id'] == pid for p in pls): return jsonify({'error': '歌单已存在'}), 409
    pls.append({'id': pid, 'type': id_type, 'name': name})
    save_playlists(pls)
    log.info(f'➕ 添加歌单：{name} ({pid})')
    threading.Thread(target=sync_playlist, args=(pid, name, id_type), daemon=True).start()
    return jsonify({'message': '添加成功，已开始同步'})

@app.route('/api/playlists/<pid>', methods=['DELETE'])
def api_del(pid):
    pls = load_playlists()
    new = [p for p in pls if p['id'] != pid]
    save_playlists(new)
    log.info(f'🗑️ 删除歌单：{pid}')
    return jsonify({'message': '删除成功'})

@app.route('/api/import', methods=['POST'])
def api_import():
    data = request.get_json()
    cookie = data.get('cookie', '').strip()
    if not cookie: return jsonify({'error': '请输入Cookie'}), 400
    imported = import_user_playlists(cookie)
    if not imported:
        return jsonify({'error': '导入失败，请查看日志排查（可能 Cookie 过期或缺少必要字段）'}), 500
    pls = load_playlists()
    existing_ids = {p['id'] for p in pls}
    new_count = 0
    for item in imported:
        if item['id'] not in existing_ids:
            pls.append({'id': item['id'], 'type': 'numeric', 'name': item['name']})
            new_count += 1
    save_playlists(pls)
    log.info(f'🎉 导入完成，新增 {new_count} 个歌单')
    return jsonify({'message': f'导入成功，新增 {new_count} 个歌单', 'playlists': pls})

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
    ids = data.get('ids', [])
    pls = load_playlists()
    for p in pls:
        if p['id'] in ids:
            threading.Thread(target=sync_playlist, args=(p['id'], p.get('name',''), p.get('type','numeric')), daemon=True).start()
    return jsonify({'message': f'开始同步 {len(ids)} 个歌单'})

if __name__ == '__main__':
    try:
        log.info('🚀 服务启动')
        threading.Thread(target=run_scheduler, daemon=True).start()
        log.info(f'🌐 面板监听 0.0.0.0:{WEB_PORT}')
        app.run(host='0.0.0.0', port=WEB_PORT, debug=False)
    except Exception as e:
        log.error(f'💥 启动失败: {traceback.format_exc()}')
        while True: time.sleep(60)
