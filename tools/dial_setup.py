"""
M5Dial に工数打刻アプリを書き込み、GAS の接続設定を保存する。

このスクリプトは手元で完結する。トークンは getpass で伏せ字入力し、
画面にもログにも残らない。

使い方(M5Dial を USB で繋いだ状態で):
    py -3.9 tools/dial_setup.py            # アプリ+設定の両方
    py -3.9 tools/dial_setup.py --app      # アプリだけ入れ直す
    py -3.9 tools/dial_setup.py --config   # 設定だけ入れ直す

事前に、COM ポートを掴んでいるもの(シリアルモニタ等)は閉じておくこと。
"""

import sys
import os
import time
import json
import getpass

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial がありません:  py -3.9 -m pip install --user pyserial")

HERE = os.path.dirname(os.path.abspath(__file__))
APP_SRC = os.path.join(HERE, "..", "dial", "main.py")
CFG_PATH = "/flash/kosu_cfg.json"
APP_PATH = "/flash/main.py"
BACKUP_PATH = "/flash/main_prev.py"


# ----------------------------------------------------------- raw REPL
def read_until(s, token, timeout=30):
    buf = b""
    t0 = time.time()
    while time.time() - t0 < timeout:
        c = s.read(4096)
        if c:
            buf += c
            if token in buf:
                return buf
        else:
            time.sleep(0.01)
    raise TimeoutError("timeout waiting for %r; tail=%r" % (token, buf[-200:]))


def raw_exec(s, code, timeout=30):
    s.reset_input_buffer()
    s.write(code.encode("utf-8") + b"\x04")
    buf = read_until(s, b"\x04>", timeout)
    if buf.startswith(b"OK"):
        buf = buf[2:]
    payload = buf[:buf.rindex(b"\x04>")]
    out, _, err = payload.partition(b"\x04")
    return (out.decode("utf-8", "replace").strip(),
            err.decode("utf-8", "replace").strip())


def connect():
    cands = [p for p in list_ports.comports() if p.vid == 0x303A]
    if not cands:
        sys.exit("M5Dial が見つかりません。USB を挿し直してください。")
    s = serial.Serial()
    s.port = cands[0].device
    s.baudrate = 115200
    s.timeout = 0.3
    s.dtr = False
    s.rts = False
    try:
        s.open()
    except Exception as e:
        sys.exit("%s を開けません: %s\nポートを掴んでいるものを閉じてください。" % (cands[0].device, e))
    time.sleep(0.3)
    s.write(b"\x03"); time.sleep(0.3); s.reset_input_buffer()
    s.write(b"\x01"); time.sleep(0.4); s.reset_input_buffer()
    out, _ = raw_exec(s, "print('ready')")
    if "ready" not in out:
        sys.exit("REPL が応答しません")
    return s, cands[0].device


def put_file(s, path, data, chunk=192):
    out, err = raw_exec(s, "f=open(%r,'wb')" % path)
    if err:
        sys.exit("書き込み開始に失敗: " + err)
    for i in range(0, len(data), chunk):
        out, err = raw_exec(s, "f.write(%r)" % data[i:i + chunk])
        if err:
            sys.exit("書き込み中に失敗(%d): %s" % (i, err))
    raw_exec(s, "f.close()")
    out, err = raw_exec(s, "f=open(%r,'rb'); d=f.read(); f.close(); print(len(d), sum(d))" % path)
    want = "%d %d" % (len(data), sum(data))
    ok = out.strip() == want
    print("   %s: %s (%d bytes)" % (path, "OK" if ok else "MISMATCH " + out, len(data)))
    return ok


# ----------------------------------------------------------- main
def main():
    do_app = "--config" not in sys.argv
    do_cfg = "--app" not in sys.argv

    url = token = None
    if do_cfg:
        print("=" * 58)
        print(" M5Dial 設定")
        print("=" * 58)
        url = input("GAS ウェブアプリの URL (/exec): ").strip()
        url = "".join(url.split())          # 折り返しの改行・空白を除去
        if not url.endswith("/exec"):
            sys.exit("URL が /exec で終わっていません。テスト用の /dev ではなくデプロイの URL を使ってください。")
        token = getpass.getpass("共有トークン (入力は表示されません): ")
        if not token:
            sys.exit("トークンが空です")

    s, port = connect()
    print("\n接続先ポート:", port)

    if do_app:
        if not os.path.exists(APP_SRC):
            sys.exit("アプリが見つかりません: " + APP_SRC)
        data = open(APP_SRC, "rb").read()
        # 既存の main.py を退避(まだ退避が無いときだけ)
        raw_exec(s, "\n".join([
            "import os",
            "names = os.listdir('/flash')",
            "if 'main.py' in names and 'main_prev.py' not in names:",
            "    f=open('/flash/main.py','rb'); d=f.read(); f.close()",
            "    g=open(%r,'wb'); g.write(d); g.close()" % BACKUP_PATH,
            "    print('backed up existing main.py')",
            "else:",
            "    print('no backup needed')",
        ]))
        print("アプリを書き込み中 ...")
        if not put_file(s, APP_PATH, data):
            sys.exit("アプリの書き込みに失敗しました")

    if do_cfg:
        blob = json.dumps({"url": url, "token": token}).encode("utf-8")
        print("設定を書き込み中 ...")
        if not put_file(s, CFG_PATH, blob):
            sys.exit("設定の書き込みに失敗しました")
        # 保存内容の確認(トークンは長さだけ)
        out, _ = raw_exec(s, "\n".join([
            "import json",
            "c = json.load(open(%r))" % CFG_PATH,
            "print('url  :', c['url'])",
            "print('token: %d文字' % len(c['token']))",
        ]))
        print(out)

    print("\n再起動します ...")
    s.write(b"\x02"); time.sleep(0.4); s.reset_input_buffer()
    s.write(b"\x04")
    buf = b""
    t0 = time.time()
    while time.time() - t0 < 35:
        buf += s.read(4096)
    s.close()
    print("-" * 58)
    sys.stdout.write(buf.decode("utf-8", "replace"))
    print("\n" + "-" * 58)
    print("完了。上の出力は Claude に貼って構いません(トークンは含まれません)。")


if __name__ == "__main__":
    main()
