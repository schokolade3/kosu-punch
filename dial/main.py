# =====================================================================
#  工数打刻 M5Dial 版   UIFlow2 / MicroPython v1.27 (M5STACK_Dial)
# ---------------------------------------------------------------------
#  スマホなしで単体動作する打刻端末。
#    - 内蔵 RFID でカードを読み、社員 / 案件 / 工程 / 休憩 を打刻する
#    - 打刻は /flash/kosu_queue.jsonl に追記し、Wi-Fi があれば GAS へ送る
#    - 圏外や電源断でもキューは残り、次に繋がったときまとめて送られる
#
#  設定ファイル(PC の tools/dial_setup.py で書き込む):
#    /flash/kosu_cfg.json   {"url": "...exec", "token": "..."}
#
#  通信の作法(実機で確認済み):
#    GAS は POST を 302 で返し、結果は Location への GET で取る。
#    POST で追うと 405 になるので、必ず GET で追うこと。
# =====================================================================

import M5, machine, network, esp32, socket, ssl, json, gc, os, time
from M5 import Widgets, Speaker, BtnA
from hardware import RFID, Rotary

CFG_PATH   = '/flash/kosu_cfg.json'
CARDS_PATH = '/flash/kosu_cards.json'
STATE_PATH = '/flash/kosu_state.json'
QUEUE_PATH = '/flash/kosu_queue.jsonl'

TZ = 9 * 3600            # JST。表示にだけ使う。保存と送信は UTC
POLL_MS = 120            # RFID ポーリング間隔
DEBOUNCE_MS = 8000       # 同じカードの連打を無視する時間
SYNC_EVERY = 60          # 秒。定期同期の間隔

BG      = 0x101018
RING_BG = 0x2a2a3a
GREEN   = 0x1e6b4a
AMBER   = 0xa8730f
BLUE    = 0x2f5d8a
PURPLE  = 0x6b3fa0
RED     = 0xb3372e
WHITE   = 0xffffff
GREY    = 0x7f8fa6

CX = CY = 120
R_IN, R_OUT = 96, 114

KIND_JA = {'employee': '社員', 'project': '案件', 'process': '工程', 'break': '休憩'}
KIND_COLOR = {'employee': BLUE, 'project': PURPLE, 'process': GREEN, 'break': AMBER}


# ------------------------------------------------------------ 小物
def jload(path, default):
    try:
        f = open(path)
        v = json.load(f)
        f.close()
        return v
    except Exception:
        return default


def jsave(path, obj):
    try:
        f = open(path, 'w')
        json.dump(obj, f)
        f.close()
        return True
    except Exception as e:
        print('save failed', path, e)
        return False


def iso(t=None):
    """UTC の ISO8601。スマホ版が送る形式と揃える。"""
    tm = time.gmtime(t if t is not None else time.time())
    return '%04d-%02d-%02dT%02d:%02d:%02d.000Z' % (tm[0], tm[1], tm[2], tm[3], tm[4], tm[5])


def hhmm(t):
    tm = time.localtime(t + TZ)
    return '%02d:%02d' % (tm[3], tm[4])


def dur(sec):
    sec = max(0, int(sec))
    return '%d:%02d:%02d' % (sec // 3600, sec % 3600 // 60, sec % 60)


def beep(f, ms=80):
    try:
        Speaker.tone(f, ms)
    except Exception:
        pass


# ------------------------------------------------------------ HTTP(GAS 用)
def _req(host, path, body, ctype='text/plain'):
    ai = socket.getaddrinfo(host, 443)[0][-1]
    sk = socket.socket()
    sk.settimeout(20)
    sk.connect(ai)
    sk = ssl.wrap_socket(sk, server_hostname=host)
    if body is None:
        req = 'GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n' % (path, host)
    else:
        req = ('POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: %s\r\n'
               'Content-Length: %d\r\nConnection: close\r\n\r\n%s'
               % (path, host, ctype, len(body), body))
    sk.write(req.encode())
    buf = b''
    while True:
        d = sk.read(1024)
        if not d:
            break
        buf += d
        if len(buf) > 20000:
            break
    sk.close()
    h, _, b = buf.partition(b'\r\n\r\n')
    return h.decode('utf-8', 'replace'), b


def _hdr(h, name):
    for line in h.split('\r\n')[1:]:
        if line.lower().startswith(name.lower() + ':'):
            return line.split(':', 1)[1].strip()
    return None


def _code(h):
    try:
        return int(h.split(' ')[1])
    except Exception:
        return 0


def _dechunk(b):
    out = b''
    while b:
        i = b.find(b'\r\n')
        if i < 0:
            break
        try:
            n = int(b[:i].split(b';')[0], 16)
        except Exception:
            return b
        if n == 0:
            break
        out += b[i + 2:i + 2 + n]
        b = b[i + 2 + n + 2:]
    return out


def gas_post(url, payload):
    """GAS へ POST し、302 は GET で追って JSON を返す。失敗時は None。"""
    try:
        host = url.split('/')[2]
        path = url[url.index(host) + len(host):]
        gc.collect()
        h, b = _req(host, path, json.dumps(payload))
        if _code(h) in (301, 302, 303, 307):
            loc = _hdr(h, 'Location')
            lhost = loc.split('/')[2]
            lpath = loc[loc.index(lhost) + len(lhost):]
            gc.collect()
            h, b = _req(lhost, lpath, None)
        if (_hdr(h, 'Transfer-Encoding') or '').lower() == 'chunked':
            b = _dechunk(b)
        if _code(h) != 200:
            print('gas http', _code(h))
            return None
        return json.loads(b.decode('utf-8', 'replace'))
    except Exception as e:
        print('gas error:', type(e).__name__, e)
        return None
    finally:
        gc.collect()


# ------------------------------------------------------------ ネットワーク
def wifi_connect(timeout=20):
    w = network.WLAN(network.STA_IF)
    w.active(True)
    if w.isconnected():
        return True
    try:
        nvs = esp32.NVS('uiflow')
        ssid = nvs.get_str('ssid0')
        pw = nvs.get_str('pswd0')
    except Exception:
        return False
    if not ssid:
        return False
    w.connect(ssid, pw)
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < timeout * 1000:
        if w.isconnected():
            return True
        time.sleep_ms(200)
    return False


def ntp_sync():
    try:
        import ntptime
        ntptime.settime()          # RTC は UTC のまま。表示時に TZ を足す
        return True
    except Exception as e:
        print('ntp failed:', e)
        return False


# ------------------------------------------------------------ キュー
def queue_append(ev):
    try:
        f = open(QUEUE_PATH, 'a')
        f.write(json.dumps(ev))
        f.write('\n')
        f.close()
    except Exception as e:
        print('queue write failed:', e)


def queue_count():
    n = 0
    try:
        f = open(QUEUE_PATH)
        for line in f:
            if line.strip():
                n += 1
        f.close()
    except Exception:
        pass
    return n


def queue_take(limit):
    """先頭から limit 件を読む。まだ消さない。"""
    rows = []
    try:
        f = open(QUEUE_PATH)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
            if len(rows) >= limit:
                break
        f.close()
    except Exception:
        pass
    return rows


def queue_drop(n):
    """送信できた先頭 n 件を捨てる。"""
    try:
        keep = []
        f = open(QUEUE_PATH)
        i = 0
        for line in f:
            if line.strip():
                i += 1
                if i > n:
                    keep.append(line if line.endswith('\n') else line + '\n')
        f.close()
        f = open(QUEUE_PATH, 'w')
        for line in keep:
            f.write(line)
        f.close()
    except Exception as e:
        print('queue drop failed:', e)


# ------------------------------------------------------------ アプリ本体
class App:
    def __init__(self):
        self.cfg = jload(CFG_PATH, {})
        self.cards = jload(CARDS_PATH, {})        # {uid: {kind, id, name}}
        self.master = jload('/flash/kosu_master.json',
                            {'employees': [], 'projects': [], 'processes': []})
        self.st = jload(STATE_PATH, {})           # {emp: {proj, proc, start, brk, brkAt}}
        self.ctx_emp = self.st.get('_ctx_emp')
        self.ctx_proj = {}                        # emp -> proj(表示用。永続化は state に含む)
        for k, v in self.st.items():
            if not k.startswith('_') and v.get('proj'):
                self.ctx_proj[k] = v['proj']
        self.ctx_proj.update(self.st.get('_ctx_proj', {}))
        self.last_uid = None
        self.last_ts = 0
        self.online = False
        self.last_sync = 0
        self.msg = ''
        self.msg_col = GREY
        self.rotary = None
        self.rfid = None
        self.mode = 'run'          # run | assign_kind | assign_item
        self.pending_uid = None
        self.sel = 0
        self.sel_kind = None
        self.msg_at = 0

    # ---------------- 保存 ----------------
    def save_state(self):
        self.st['_ctx_emp'] = self.ctx_emp
        self.st['_ctx_proj'] = self.ctx_proj
        jsave(STATE_PATH, self.st)

    # ---------------- 名前解決 ----------------
    def card(self, uid):
        return self.cards.get(uid)

    def name_of(self, kind, ident):
        for o in self.master.get(kind + 's' if kind != 'process' else 'processes', []):
            if o.get('id') == ident:
                return o.get('name', ident)
        return ident or '-'

    def emp_name(self, i):
        return self.name_of('employee', i) if i else '-'

    def proj_name(self, i):
        if not i:
            return '-'
        for o in self.master.get('projects', []):
            if o.get('id') == i:
                c = o.get('code') or ''
                return (c + ' ' + o.get('name', '')).strip()
        return i

    def proc_name(self, i):
        return self.name_of('process', i) if i else '-'

    # ---------------- 打刻 ----------------
    def emit(self, typ, emp, proj, proc):
        ev = {
            'id': '%s%d' % (''.join('%02x' % b for b in os.urandom(3)), time.time()),
            'ts': iso(), 'type': typ,
            'emp': emp or '', 'empName': self.emp_name(emp) if emp else '',
            'proj': proj or '', 'projCode': '', 'projName': self.proj_name(proj) if proj else '',
            'proc': proc or '', 'procName': self.proc_name(proc) if proc else '',
            'src': 'dial',
        }
        queue_append(ev)

    def punch(self, uid):
        now = time.time()
        c = self.card(uid)
        if not c:
            return self.on_unknown(uid)

        if uid == self.last_uid and (time.ticks_ms() - self.last_ts) < DEBOUNCE_MS:
            beep(320); self.say('連続読み取りを無視', RED); return
        self.last_uid, self.last_ts = uid, time.ticks_ms()

        kind = c['kind']
        if kind == 'employee':
            self.ctx_emp = c['id']; self.save_state()
            beep(660); self.say('社員: ' + self.emp_name(c['id']), BLUE); return

        if not self.ctx_emp:
            beep(320); self.say('先に社員カードを', RED); return

        if kind == 'project':
            self.ctx_proj[self.ctx_emp] = c['id']
            s = self.st.get(self.ctx_emp)
            if s:                                  # 作業中なら案件変更で区切る
                self.emit('process', self.ctx_emp, s['proj'], s['proc'])
                self.st.pop(self.ctx_emp, None)
            self.emit('project', self.ctx_emp, c['id'], None)
            self.save_state()
            beep(660); self.say('案件: ' + self.proj_name(c['id']), PURPLE); return

        s = self.st.get(self.ctx_emp)

        if kind == 'break':
            if not s:
                beep(320); self.say('作業中ではありません', RED); return
            if s.get('brkAt'):
                s['brk'] = s.get('brk', 0) + (now - s['brkAt']); s['brkAt'] = None
                self.emit('break', self.ctx_emp, s['proj'], s['proc'])
                beep(880); self.say('休憩おわり', GREEN)
            else:
                s['brkAt'] = now
                self.emit('break', self.ctx_emp, s['proj'], s['proc'])
                beep(660); self.say('休憩はじめ', AMBER)
            self.save_state(); return

        # 工程
        proj = self.ctx_proj.get(self.ctx_emp)
        if not proj:
            beep(320); self.say('先に案件カードを', RED); return
        pid = c['id']
        if s and s.get('brkAt'):                   # 休憩明け
            s['brk'] = s.get('brk', 0) + (now - s['brkAt']); s['brkAt'] = None
            self.emit('break', self.ctx_emp, s['proj'], s['proc'])
            if s['proc'] == pid and s['proj'] == proj:
                self.save_state(); beep(880); self.say('休憩おわり・再開', GREEN); return
        if not s:
            self.emit('process', self.ctx_emp, proj, pid)
            self.st[self.ctx_emp] = {'proj': proj, 'proc': pid, 'start': now, 'brk': 0, 'brkAt': None}
            beep(880); self.say(self.proc_name(pid) + ' 開始', GREEN)
        elif s['proc'] == pid and s['proj'] == proj:
            self.emit('process', self.ctx_emp, proj, pid)
            self.st.pop(self.ctx_emp, None)
            beep(880); self.say(self.proc_name(pid) + ' 終了', GREY)
        else:
            self.emit('process', self.ctx_emp, s['proj'], s['proc'])
            self.emit('process', self.ctx_emp, proj, pid)
            self.st[self.ctx_emp] = {'proj': proj, 'proc': pid, 'start': now, 'brk': 0, 'brkAt': None}
            beep(880); self.say('切替 → ' + self.proc_name(pid), GREEN)
        self.save_state()

    # ---------------- 未登録カード ----------------
    def on_unknown(self, uid):
        beep(320)
        self.pending_uid = uid
        self.mode = 'assign_kind'
        self.sel = 0
        self.say('未登録カード', RED)

    def assign(self, kind, ident, name):
        self.cards[self.pending_uid] = {'kind': kind, 'id': ident, 'name': name}
        jsave(CARDS_PATH, self.cards)
        # シート側にも知らせる(送信できなくてもキューに残る)
        queue_append({'id': 'card' + self.pending_uid.replace(':', ''), 'ts': iso(),
                      'type': 'card', 'uid': self.pending_uid, 'kind': kind,
                      'refId': ident or '', 'name': name, 'src': 'dial'})
        self.pending_uid = None
        self.mode = 'run'
        beep(880); self.say('登録: ' + name, GREEN)

    # ---------------- 同期 ----------------
    def sync(self, silent=True):
        url = self.cfg.get('url')
        if not url:
            if not silent:
                self.say('設定がありません', RED)
            return
        if not wifi_connect(8):
            self.online = False
            if not silent:
                self.say('Wi-Fi に繋がりません', RED)
            return
        self.online = True
        rows = queue_take(25)
        if not rows:
            if not silent:
                self.say('送るものがありません', GREY)
            self.last_sync = time.time()
            return
        res = gas_post(url, {'token': self.cfg.get('token', ''), 'events': rows,
                             'deleted': [], 'projects': []})
        if res and res.get('ok'):
            queue_drop(len(rows))
            self.last_sync = time.time()
            if not silent:
                self.say('同期 %d件' % len(rows), GREEN)
        else:
            err = (res or {}).get('error', '通信失敗')
            if not silent:
                self.say('同期失敗: ' + str(err)[:16], RED)

    def fetch_master(self):
        url = self.cfg.get('url')
        if not url or not wifi_connect(8):
            return False
        res = gas_post(url, {'token': self.cfg.get('token', ''), 'action': 'masters'})
        if res and res.get('ok') and res.get('master'):
            self.master = res['master']
            jsave('/flash/kosu_master.json', self.master)
            if res.get('cards'):
                self.cards.update(res['cards'])
                jsave(CARDS_PATH, self.cards)
            return True
        return False

    # ---------------- 表示 ----------------
    def say(self, text, col=GREY):
        self.msg = text
        self.msg_col = col
        self.msg_at = time.time()

    # ---------------- 割り当ての選択肢 ----------------
    def kind_choices(self):
        return [('employee', '社員'), ('project', '案件'),
                ('process', '工程'), ('break', '休憩'), (None, 'やめる')]

    def item_choices(self):
        key = {'employee': 'employees', 'project': 'projects', 'process': 'processes'}[self.sel_kind]
        items = [(o.get('id'), (o.get('code', '') + ' ' + o.get('name', '')).strip()
                  if self.sel_kind == 'project' else o.get('name', ''))
                 for o in self.master.get(key, [])]
        items.append((None, 'やめる'))
        return items


# ------------------------------------------------------------ 画面
def ring(color):
    M5.Display.fillArc(CX, CY, R_IN, R_OUT, 0, 360, color)


def clear_center():
    M5.Display.fillCircle(CX, CY, R_IN - 2, BG)


class Screen:
    def __init__(self):
        M5.Display.setRotation(0)
        M5.Display.clear(BG)
        F18, F24 = Widgets.FONTS.DejaVu18, Widgets.FONTS.DejaVu24
        self.emp  = Widgets.Label('', 20, 40,  1.0, BLUE,  BG, F18)
        self.proc = Widgets.Label('', 20, 76,  1.5, WHITE, BG, F24)
        self.proj = Widgets.Label('', 20, 122, 1.0, GREY,  BG, F18)
        self.tim  = Widgets.Label('', 20, 148, 1.0, WHITE, BG, F18)
        self.msg  = Widgets.Label('', 20, 180, 1.0, GREY,  BG, F18)
        self.net  = Widgets.Label('', 20, 16,  1.0, GREY,  BG, F18)
        self.last = {}
        self.ring_col = None

    def set(self, key, lbl, text, col=None):
        if self.last.get(key) != text:
            if col is not None:
                lbl.setColor(col, BG)
            lbl.setText(text)
            self.last[key] = text

    def set_ring(self, col):
        if self.ring_col != col:
            ring(col)
            self.ring_col = col

    def wipe(self):
        clear_center()
        self.last.clear()


def center(s, per=11):
    """雑だが実用十分な中央寄せ。文字数から左端を決める。"""
    return max(14, CX - int(len(s) * per / 2))


def draw_run(app, scr):
    emp = app.ctx_emp
    s = app.st.get(emp) if emp else None
    unsent = queue_count()

    scr.set('net', scr.net, ('ONLINE' if app.online else 'OFFLINE') +
            (' +%d' % unsent if unsent else ''), GREEN if app.online else GREY)

    e = app.emp_name(emp) if emp else '社員カードを'
    scr.set('emp', scr.emp, e[:12])

    if s:
        on_break = bool(s.get('brkAt'))
        scr.set_ring(AMBER if on_break else GREEN)
        scr.set('proc', scr.proc, app.proc_name(s['proc'])[:8], AMBER if on_break else WHITE)
        scr.set('proj', scr.proj, app.proj_name(s['proj'])[:16])
        base = s['brkAt'] if on_break else s['start']
        el = time.time() - base - (0 if on_break else s.get('brk', 0))
        scr.set('time', scr.tim, ('休憩 ' if on_break else '') + dur(el))
    else:
        scr.set_ring(RING_BG)
        pj = app.ctx_proj.get(emp) if emp else None
        scr.set('proc', scr.proc, '待機中', GREY)
        scr.set('proj', scr.proj, app.proj_name(pj)[:16] if pj else '案件カードを')
        scr.set('time', scr.tim, '')

    m = app.msg if (time.time() - app.msg_at) < 6 else ''
    scr.set('msg', scr.msg, m[:16], app.msg_col)


def draw_menu(app, scr, title, choices):
    scr.set_ring(BLUE)
    scr.set('emp', scr.emp, title)
    n = len(choices)
    i = app.sel % n
    prev = choices[(i - 1) % n][1]
    cur = choices[i][1]
    nxt = choices[(i + 1) % n][1]
    scr.set('proj', scr.proj, prev[:14])
    scr.set('proc', scr.proc, ('> ' + cur)[:11], WHITE)
    scr.set('time', scr.tim, nxt[:14])
    scr.set('msg', scr.msg, '回して選び押して決定', GREY)
    scr.set('net', scr.net, app.pending_uid[-11:] if app.pending_uid else '')


# ------------------------------------------------------------ 起動
def boot():
    M5.begin()
    try:
        Speaker.begin(); Speaker.setVolume(70)
    except Exception:
        pass
    scr = Screen()
    scr.set_ring(RING_BG)
    scr.set('proc', scr.proc, '起動中', GREY)

    a = App()
    try:
        a.rotary = Rotary()
    except Exception as e:
        print('rotary:', e)
    try:
        a.rfid = RFID()
    except Exception as e:
        print('rfid:', e)
        scr.set('msg', scr.msg, 'RFID 初期化失敗', RED)

    scr.set('msg', scr.msg, 'Wi-Fi 接続中', GREY)
    if wifi_connect(15):
        a.online = True
        scr.set('msg', scr.msg, '時刻同期中', GREY)
        ntp_sync()
        scr.set('msg', scr.msg, 'マスタ取得中', GREY)
        a.fetch_master()
    else:
        scr.set('msg', scr.msg, 'オフラインで開始', AMBER)
    beep(1200, 60)
    a.say('カードをかざしてください')
    scr.wipe()
    return a, scr


def main():
    a, scr = boot()
    rot_last = a.rotary.get_rotary_value() if a.rotary else 0
    tick = 0

    while True:
        M5.update()

        # --- カード読み取り ---
        if a.rfid:
            try:
                if a.rfid.is_new_card_present():
                    raw = a.rfid.read_card_uid()
                    if raw:
                        uid = ':'.join('%02x' % b for b in raw)
                        if a.mode == 'run':
                            a.punch(uid)
                        scr.wipe()
            except Exception as e:
                print('rfid read:', e)

        # --- ロータリー ---
        if a.rotary:
            try:
                v = a.rotary.get_rotary_value()
            except Exception:
                v = rot_last
            if v != rot_last:
                if a.mode != 'run':
                    a.sel += (1 if v > rot_last else -1)
                    beep(1800, 6)
                rot_last = v

        # --- 決定 / 手動同期 ---
        pressed = False
        try:
            pressed = BtnA.wasClicked()
        except Exception:
            pass
        if pressed:
            if a.mode == 'assign_kind':
                kind, _ = a.kind_choices()[a.sel % len(a.kind_choices())]
                if kind is None:
                    a.mode = 'run'; a.pending_uid = None; a.say('やめました')
                elif kind == 'break':
                    a.assign('break', None, '休憩')
                else:
                    a.sel_kind = kind; a.sel = 0; a.mode = 'assign_item'
                scr.wipe()
            elif a.mode == 'assign_item':
                ch = a.item_choices()
                ident, name = ch[a.sel % len(ch)]
                if ident is None:
                    a.mode = 'run'; a.pending_uid = None; a.say('やめました')
                else:
                    a.assign(a.sel_kind, ident, name)
                scr.wipe()
            else:
                a.say('同期中…'); draw_run(a, scr)
                a.sync(silent=False)
                scr.wipe()

        # --- 描画 ---
        if a.mode == 'run':
            draw_run(a, scr)
        elif a.mode == 'assign_kind':
            draw_menu(a, scr, '未登録カード', a.kind_choices())
        else:
            draw_menu(a, scr, KIND_JA[a.sel_kind] + 'を選ぶ', a.item_choices())

        # --- 定期同期 ---
        tick += 1
        if a.mode == 'run' and tick % 40 == 0:
            if queue_count() and (time.time() - a.last_sync) > SYNC_EVERY:
                a.sync(silent=True)
                gc.collect()

        time.sleep_ms(POLL_MS)


try:
    main()
except Exception as e:
    try:
        M5.Display.clear(BG)
        M5.Display.setCursor(10, 100)
        M5.Display.setTextColor(RED, BG)
        M5.Display.print('ERROR', RED)
        M5.Display.setCursor(10, 130)
        M5.Display.print(str(e)[:28], RED)
    except Exception:
        pass
    print('fatal:', e)
    raise
