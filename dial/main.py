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

import gc, esp32


def hp(tag):
    """ヒープの内訳を出す。TLS は ESP-IDF 側のヒープを使うので、
    mpy free だけ見ていても足りるかどうか分からない。"""
    try:
        i = esp32.idf_heap_info(esp32.HEAP_DATA)
        gc.collect()
        print('heap %-9s idf=%6d largest=%6d mpy=%6d'
              % (tag, sum(r[1] for r in i), max(r[2] for r in i), gc.mem_free()))
    except Exception:
        pass


import machine, network, socket, ssl, json, os, time

# M5 系は「あとで」読み込む。
# Wi-Fi は ESP-IDF ヒープを 48〜66KB 確保するが、確保できる量は
# そのとき空いている連続領域に左右される。M5 を先に読み込んでから
# Wi-Fi を張ると残りが 35KB まで落ち、mbedTLS(合計40KB前後が必要)が
# ENOMEM で失敗する。Wi-Fi を先に張れば全部載せても 52KB 残る。
M5 = Widgets = Speaker = BtnA = RFID = Rotary = None
FONT_JA = FONT_S = FONT_N = None


def load_ui():
    """Wi-Fi 接続後に呼ぶこと。順序を逆にすると通信できなくなる。"""
    global M5, Widgets, Speaker, BtnA, RFID, Rotary, FONT_JA, FONT_S, FONT_N
    import M5 as _M5
    from M5 import Widgets as _W, Speaker as _Sp, BtnA as _B
    from hardware import RFID as _R, Rotary as _Ro
    M5, Widgets, Speaker, BtnA, RFID, Rotary = _M5, _W, _Sp, _B, _R, _Ro
    FONT_JA = Widgets.FONTS.EFontJA24      # 日本語。24px しか無い
    FONT_S = Widgets.FONTS.Montserrat14    # 英数字の小さい行
    FONT_N = Widgets.FONTS.DejaVu24        # 経過時間などの数字

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

UNSET = ''              # 社員・案件が未設定のときの値。集計側でも空文字で通す
NAME_UNSET = '未設定'

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
    if Speaker is None:
        return
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
        sk.write(('GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n'
                  % (path, host)).encode())
    else:
        # Content-Length は「バイト数」。文字数で数えると日本語が入った瞬間に
        # 本文が途中で切られ、受け側で JSON パースエラーになる。
        bb = body.encode('utf-8') if isinstance(body, str) else body
        sk.write(('POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: %s\r\n'
                  'Content-Length: %d\r\nConnection: close\r\n\r\n'
                  % (path, host, ctype, len(bb))).encode())
        sk.write(bb)
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


MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def clock_ok():
    return time.gmtime()[0] >= 2024


def set_clock_from_http(d):
    """HTTP の Date ヘッダ(GMT)で時計を合わせる。
    ntptime は ESP-IDF ヒープを19KB確保したまま返さず、その後の TLS が
    ENOMEM になる。どうせ GAS と通信するので、その応答で合わせる。"""
    try:
        p = d.split(' ')                    # 'Mon, 08 Sep 2026 01:23:45 GMT'
        yr, mon, day = int(p[3]), MONTHS.index(p[2]) + 1, int(p[1])
        hh, mm, ss = [int(x) for x in p[4].split(':')]
        machine.RTC().datetime((yr, mon, day, 0, hh, mm, ss, 0))
        return True
    except Exception as e:
        print('clock parse failed:', e)
        return False


def gas_post(url, payload):
    """GAS へ POST し、302 は GET で追って JSON を返す。失敗時は None。"""
    try:
        host = url.split('/')[2]
        path = url[url.index(host) + len(host):]
        gc.collect()
        h, b = _req(host, path, json.dumps(payload))
        if not clock_ok():
            d = _hdr(h, 'Date')
            if d and set_clock_from_http(d):
                print('clock set from HTTP Date')
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
        for k, v in self.st.items():          # 旧形式の保存状態を補う
            if not k.startswith('_') and isinstance(v, dict) and 'emp' not in v:
                v['emp'] = k
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
        self.unsent = 0        # 未送信件数。毎フレーム数え直さない

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
        return self.name_of('employee', i) if i else NAME_UNSET

    def proj_name(self, i):
        if not i:
            return NAME_UNSET
        for o in self.master.get('projects', []):
            if o.get('id') == i:
                c = o.get('code') or ''
                return (c + ' ' + o.get('name', '')).strip()
        return i

    def proc_name(self, i):
        return self.name_of('process', i) if i else NAME_UNSET

    # ---------------- 打刻 ----------------
    def emit(self, typ, emp, proj, proc):
        """イベントをキューに積み、その辞書を返す。
        返り値を持っておくと、あとで同じ id で送り直して内容を訂正できる。"""
        ev = {
            'id': '%s%d' % (''.join('%02x' % b for b in os.urandom(3)), time.time()),
            'ts': iso(), 'type': typ,
            'emp': emp or '', 'empName': self.emp_name(emp) if emp else '',
            'proj': proj or '', 'projCode': '', 'projName': self.proj_name(proj) if proj else '',
            'proc': proc or '', 'procName': self.proc_name(proc) if proc else '',
            'src': 'dial',
        }
        queue_append(ev)
        self.unsent += 1
        return ev

    def repatch(self, s):
        """進行中セッションの開始イベントを、同じ id で送り直す。
        GAS は id で上書きするので、あとから社員や案件が判明しても
        記録が分断されず、最初から正しい紐づけになる。"""
        ev = s.get('ev')
        if not ev:
            return
        ev['emp'] = s['emp']
        ev['empName'] = self.emp_name(s['emp']) if s['emp'] else ''
        ev['proj'] = s['proj']
        ev['projName'] = self.proj_name(s['proj']) if s['proj'] else ''
        queue_append(ev)
        self.unsent += 1

    def nag(self, what):
        """作業は止めずに、足りないカードを知らせる。低音を2回。"""
        beep(440, 70)
        time.sleep_ms(110)
        beep(440, 70)
        self.say(what + 'カードを', RED)

    # ---- カード種別ごとの処理 ----
    def set_employee(self, eid):
        prev = self.ctx_emp
        self.ctx_emp = eid
        # 社員未設定のまま走っているセッションがあれば、それを引き取る
        s = self.st.get(UNSET)
        if s and not s['emp']:
            self.st.pop(UNSET, None)
            s['emp'] = eid
            self.st[eid] = s
            self.ctx_proj[eid] = self.ctx_proj.get(eid) or self.ctx_proj.get(UNSET) or UNSET
            self.repatch(s)
            self.save_state()
            beep(880); self.say(self.emp_name(eid) + 'に紐づけ', GREEN)
            return
        self.save_state()
        beep(660); self.say('社員: ' + self.emp_name(eid), BLUE)

    def set_project(self, pid):
        emp = self.ctx_emp or UNSET
        s = self.st.get(emp)
        if s and not s['proj']:
            # 案件未設定で走っているセッションを、遡って紐づける
            s['proj'] = pid
            self.ctx_proj[emp] = pid
            self.repatch(s)
            self.save_state()
            beep(880); self.say(self.proj_name(pid) + 'に紐づけ', GREEN)
            return
        if s and s['proj'] != pid:
            # 案件が変わるなら、いまの作業はそこで区切る
            self.emit('process', emp, s['proj'], s['proc'])
            self.st.pop(emp, None)
        self.ctx_proj[emp] = pid
        self.emit('project', emp, pid, None)
        self.save_state()
        beep(660); self.say('案件: ' + self.proj_name(pid), PURPLE)

    def punch(self, uid):
        now = time.time()
        c = self.card(uid)
        if not c:
            return self.on_unknown(uid)

        if uid == self.last_uid and (time.ticks_ms() - self.last_ts) < DEBOUNCE_MS:
            beep(320); self.say('連続読み無視', RED); return
        self.last_uid, self.last_ts = uid, time.ticks_ms()

        kind = c['kind']
        if kind == 'employee':
            return self.set_employee(c['id'])
        if kind == 'project':
            return self.set_project(c['id'])

        # 社員・案件が未設定でも作業時間の記録は始める。
        # 取りこぼすより、あとから紐づけられる形で記録するほうがよい。
        emp = self.ctx_emp or UNSET
        s = self.st.get(emp)

        if kind == 'break':
            if not s:
                beep(320); self.say('作業中でない', RED); return
            if s.get('brkAt'):
                s['brk'] = s.get('brk', 0) + (now - s['brkAt']); s['brkAt'] = None
                self.emit('break', emp, s['proj'], s['proc'])
                beep(880); self.say('休憩おわり', GREEN)
            else:
                s['brkAt'] = now
                self.emit('break', emp, s['proj'], s['proc'])
                beep(660); self.say('休憩はじめ', AMBER)
            self.save_state(); return

        # 工程
        proj = self.ctx_proj.get(emp) or UNSET
        pid = c['id']
        if s and s.get('brkAt'):                   # 休憩明け
            s['brk'] = s.get('brk', 0) + (now - s['brkAt']); s['brkAt'] = None
            self.emit('break', emp, s['proj'], s['proc'])
            if s['proc'] == pid and s['proj'] == proj:
                self.save_state(); beep(880); self.say('休憩おわり', GREEN); return

        if not s:
            ev = self.emit('process', emp, proj, pid)
            self.st[emp] = {'emp': emp, 'proj': proj, 'proc': pid,
                            'start': now, 'brk': 0, 'brkAt': None, 'ev': ev}
            beep(880); self.say(self.proc_name(pid) + ' 開始', GREEN)
        elif s['proc'] == pid and s['proj'] == proj:
            self.emit('process', emp, proj, pid)
            self.st.pop(emp, None)
            beep(880); self.say(self.proc_name(pid) + ' 終了', GREY)
        else:
            self.emit('process', emp, s['proj'], s['proc'])
            ev = self.emit('process', emp, proj, pid)
            self.st[emp] = {'emp': emp, 'proj': proj, 'proc': pid,
                            'start': now, 'brk': 0, 'brkAt': None, 'ev': ev}
            beep(880); self.say('切替 → ' + self.proc_name(pid), GREEN)
        self.save_state()

        # 足りないものがあれば、記録は続けたまま知らせる
        if emp == UNSET:
            self.nag('社員')
        elif proj == UNSET:
            self.nag('案件')

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
        self.unsent += 1
        self.pending_uid = None
        self.mode = 'run'
        beep(880); self.say('登録: ' + name, GREEN)

    # ---------------- 同期 ----------------
    def sync(self, silent=True):
        url = self.cfg.get('url')
        if not url:
            if not silent:
                self.say('設定なし', RED)
            return
        if not wifi_connect(8):
            self.online = False
            if not silent:
                self.say('Wi-Fi不可', RED)
            return
        self.online = True
        rows = queue_take(25)
        if not rows:
            if not silent:
                self.say('送信なし', GREY)
            self.last_sync = time.time()
            return
        res = gas_post(url, {'token': self.cfg.get('token', ''), 'events': rows,
                             'deleted': [], 'projects': []})
        if res and res.get('ok'):
            queue_drop(len(rows))
            self.unsent = queue_count()
            self.last_sync = time.time()
            if not silent:
                self.say('同期 %d件' % len(rows), GREEN)
        else:
            err = (res or {}).get('error')
            msg = ('トークン不一致' if err == 'bad token'
                   else ('通信できない' if res is None else '同期失敗'))
            if not silent:
                self.say(msg, RED)
            print('sync failed:', err)

    def fetch_master(self):
        url = self.cfg.get('url')
        if not url or not wifi_connect(8):
            return False
        res = gas_post(url, {'token': self.cfg.get('token', ''), 'action': 'masters'})
        if res is None:
            self.say('通信できない', RED)
            return False
        if not res.get('ok'):
            # 黙って失敗すると原因が分からないので画面に出す
            self.say('トークン不一致' if res.get('error') == 'bad token' else 'GASエラー', RED)
            print('master fetch rejected:', res.get('error'))
            return False
        if res.get('master'):
            self.master = res['master']
            jsave('/flash/kosu_master.json', self.master)
            if res.get('cards'):
                self.cards.update(res['cards'])
                jsave(CARDS_PATH, self.cards)
            print('master ok: emp=%d proj=%d proc=%d cards=%d' % (
                len(self.master.get('employees', [])), len(self.master.get('projects', [])),
                len(self.master.get('processes', [])), len(res.get('cards') or {})))
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
#
# 丸いディスプレイなので、行ごとに使える横幅が違う。
# 半径 92px の安全域における弦の長さから、各行の左右余白を決めてある。
#   (中心y, 行の高さ, 片側の幅)
BANDS = ((52, 26, 60), (86, 30, 84), (120, 34, 90), (154, 30, 84), (186, 28, 62))

def fit(text, maxw):
    """実測幅で切り詰める。文字数で切ると全角半角が混ざったときに破綻する。"""
    if M5.Display.textWidth(text) <= maxw:
        return text
    while text and M5.Display.textWidth(text + '…') > maxw:
        text = text[:-1]
    return text + '…' if text else ''


class Screen:
    def __init__(self):
        M5.Display.setRotation(0)
        M5.Display.fillScreen(BG)
        self.ring_col = None
        self.cache = {}

    def ring(self, col):
        """外周リング。塗り分けは円2枚で行う(fillArc の 0-360 は挙動が怪しい)。"""
        if col == self.ring_col:
            return
        M5.Display.fillCircle(CX, CY, R_OUT, col)
        M5.Display.fillCircle(CX, CY, R_IN, BG)
        self.ring_col = col
        self.cache.clear()          # 内側を塗り直したので文字も描き直す

    def line(self, idx, text, col=WHITE, font=None, fh=24):
        if self.cache.get(idx) == (text, col):
            return
        font = font or FONT_JA
        cy, h, half = BANDS[idx]
        M5.Display.fillRect(CX - half, cy - h // 2, half * 2, h, BG)
        if text:
            M5.Display.setFont(font)
            M5.Display.setTextColor(col, BG)
            t = fit(str(text), half * 2 - 6)
            M5.Display.drawString(t, CX - M5.Display.textWidth(t) // 2, cy - fh // 2)
        self.cache[idx] = (text, col)

    def wipe(self):
        M5.Display.fillCircle(CX, CY, R_IN, BG)
        self.cache.clear()


def draw_run(app, scr):
    emp = app.ctx_emp
    s = app.st.get(emp or UNSET)
    unsent = app.unsent

    scr.line(0, ('ONLINE' if app.online else 'OFFLINE') + (' +%d' % unsent if unsent else ''),
             GREEN if app.online else GREY, FONT_S, 14)

    # 社員行。作業中なのに未設定なら赤で促す
    if s and not s['emp']:
        scr.line(1, NAME_UNSET, RED)
    elif emp:
        scr.line(1, app.emp_name(emp), BLUE)
    else:
        scr.line(1, '社員カードを', GREY)

    showing_msg = (time.time() - app.msg_at) < 6 and app.msg

    if s:
        on_break = bool(s.get('brkAt'))
        scr.ring(AMBER if on_break else GREEN)
        scr.line(2, app.proc_name(s['proc']), AMBER if on_break else WHITE)
        base = s['brkAt'] if on_break else s['start']
        el = time.time() - base - (0 if on_break else s.get('brk', 0))
        bottom = ('休憩 ' if on_break else '') + dur(el)
        third = app.proj_name(s['proj'])
        third_col = RED if not s['proj'] else GREY
    else:
        scr.ring(RING_BG)
        pj = app.ctx_proj.get(emp or UNSET)
        scr.line(2, '待機中', GREY)
        bottom = ''
        third = app.proj_name(pj) if pj else '案件カードを'
        third_col = GREY

    # メッセージは幅に余裕のある3行目に出す(最下段は狭くて4文字ほどしか入らない)
    if showing_msg:
        scr.line(3, app.msg, app.msg_col)
    else:
        scr.line(3, third, third_col)
    scr.line(4, bottom, WHITE, FONT_N, 24)


def draw_menu(app, scr, title, choices):
    scr.ring(BLUE)
    n = len(choices)
    i = app.sel % n
    scr.line(0, app.pending_uid[-11:] if app.pending_uid else '', GREY, FONT_S, 14)
    scr.line(1, title, GREY)
    scr.line(2, choices[i][1], WHITE)
    scr.line(3, choices[(i + 1) % n][1], GREY)
    scr.line(4, '回して選ぶ', GREY)


# ------------------------------------------------------------ 起動
def boot():
    """初期化の順序が性能を決める。
    Wi-Fi は約66KB、Speaker は約29KB、RFID は約10KB の ESP-IDF ヒープを
    確保したまま返さない。TLS は 20KB 前後の連続領域を要求するため、
    ネットワーク処理(Wi-Fi/NTP/マスタ取得)を先に済ませてから
    音とセンサーを初期化する。逆順にすると TLS が ENOMEM で通らない。"""
    hp('start')
    a = App()

    # --- 通信を先に済ませる。画面はまだ立ち上げない ---
    print('connecting wifi ...')
    if wifi_connect(15):
        a.online = True
        hp('wifi')
        a.fetch_master()          # ここで HTTP の Date から時計も合う
        hp('master')
        if not clock_ok():        # GAS に届かなかったときの最後の手段
            ntp_sync()
            hp('ntp')
    print('wifi=%s clock=%s' % (a.online, clock_ok()))

    # --- ここから画面とセンサー ---
    load_ui()
    hp('ui-import')
    M5.begin()
    scr = Screen()
    scr.ring(RING_BG)
    scr.line(2, '起動中', GREY)
    hp('display')

    try:
        a.rotary = Rotary()
    except Exception as e:
        print('rotary:', e)
    try:
        a.rfid = RFID()
    except Exception as e:
        print('rfid:', e)
        scr.line(3, 'RFID 失敗', RED)
    hp('sensors')
    try:
        Speaker.begin(); Speaker.setVolume(70)
    except Exception:
        pass
    hp('speaker')

    if not a.online:
        scr.line(3, 'オフライン', AMBER)
    elif not clock_ok():
        scr.line(3, '時刻未設定', RED)
    a.unsent = queue_count()
    beep(1200, 60)
    a.say('カードを')
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
                a.fetch_master()      # スマホ側で登録したカードを取り込む
                a.sync(silent=False)  # こちらの打刻とカードを送る
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
            if a.unsent and (time.time() - a.last_sync) > SYNC_EVERY:
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
