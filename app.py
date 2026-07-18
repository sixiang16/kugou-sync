def get_user_playlists():
    """自动获取当前账号的所有歌单（优先移动端接口，失败则用PC端）"""
    # 从 Cookie 中提取用户 ID（必须有）
    userid = session.cookies.get('KugooID', '').strip()
    if not userid:
        add_log('⚠️ Cookie 中未找到 KugooID，无法获取歌单')
        return []

    # 方案1：移动端接口（需要 userid）
    url = 'https://mobilecdn.kugou.com/api/v3/playlist/getsonglist'
    params = {
        'format': 'json',
        'userid': userid,
        'page': 1,
        'pagesize': 500
    }
    try:
        r = session.get(url, params=params, timeout=15,
                        headers={'Referer': 'https://m.kugou.com'})
        data = r.json()
        if data.get('status') == 1 and data['data'].get('info'):
            add_log(f'✅ 通过移动端接口获取到歌单')
            return data['data']['info']
        else:
            add_log(f'⚠️ 移动端接口返回异常: {data}')
    except Exception as e:
        add_log(f'❌ 移动端接口请求失败: {e}')

    # 方案2：PC 网页端接口（备用，不需要额外参数）
    url_pc = 'https://wwwapi.kugou.com/yy/index.php'
    params_pc = {
        'r': 'play/getUserPlaylist',
        'uid': userid
    }
    try:
        r = session.get(url_pc, params=params_pc, timeout=15,
                        headers={'Referer': 'https://www.kugou.com'})
        data = r.json()
        if data.get('err_code') == 0 and data.get('data'):
            add_log(f'✅ 通过PC端接口获取到歌单')
            return data['data']
        else:
            add_log(f'⚠️ PC端接口返回异常: {data}')
    except Exception as e:
        add_log(f'❌ PC端接口请求失败: {e}')

    add_log('⛔ 两种接口均未能获取歌单，请确认 Cookie 已更新且账号有歌单')
    return []
