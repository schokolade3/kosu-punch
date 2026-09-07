"""
M5Dial に工数打刻アプリを書き込み、GAS の接続設定を保存する。

トークンは伏せ字入力で、画面にもログにも残らない。

使い方(M5Dial を USB で繋いだ状態で):
    py -3.9 tools/dial_setup.py            # アプリ + 設定
    py -3.9 tools/dial_setup.py --app      # アプリだけ
    py -3.9 tools/dial_setup.py --config   # 設定だけ

事前に、COM ポートを掴んでいるもの(シリアルモニタ等)は閉じておくこと。

なぜ .mpy を使うのか
    ソースのまま置くと、MicroPython が 27KB のファイルをコンパイル・保持する
    ために ESP-IDF ヒープを 65KB 奪い、TLS が ENOMEM で失敗する。
    事前にバイトコード化すると、その 65KB がまるごと通信に回せる。
"""

import sys
import os
import time
import json
import getpass
import subprocess

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial がありません:  py -3.9 -m pip install --user pyserial")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
APP_SRC = os.path.join(ROOT, "dial", "main.py")
MPY_OUT = os.path.join(ROOT, "build", "kosu.mpy")
DEV_MPY = "/flash/libs/kosu.mpy"
DEV_MAIN = "/flash/main.py"
CFG_PATH = "/flash/kosu_cfg.json"

STUB = "# main.py: 本体は /flash/libs/kosu.mpy\nimport kosu\n".encode("utf-8")


# ----------------------------------------------------------- raw REPL
def read_until(s, token, timeout=60):
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


def raw_exec(s, code, timeout=60):
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
        sys.exit("%s を開けません: %s\nポートを掴んでいるものを閉じてください。"
                 % (cands[0].device, e))
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
    # 24KB を一括で読むとデバイス側がメモリ不足になるので分割して照合する
    code = ("f=open(%r,'rb')\nt=0; n=0\nwhile True:\n    b=f.read(512)\n"
            "    if not b: break\n    n+=len(b); t+=sum(b)\nf.close()\nprint(n, t)") % path
    out, err = raw_exec(s, code)
    ok = out.strip() == "%d %d" % (len(data), sum(data))
    print("   %-24s %s (%d bytes)" % (path, "OK" if ok else "MISMATCH " + out, len(data)))
    return ok


def build_mpy():
    """mpy-cross があればビルドし、無ければリポジトリ同梱の build/kosu.mpy を使う。"""
    try:
        import mpy_cross
        os.makedirs(os.path.dirname(MPY_OUT), exist_ok=True)
        r = subprocess.run([str(mpy_cross.mpy_cross), "-O2", "-o", MPY_OUT, APP_SRC],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("mpy-cross 失敗:", r.stderr.strip())
        else:
            print("ビルド:", MPY_OUT, os.path.getsize(MPY_OUT), "bytes")
            return MPY_OUT
    except ImportError:
        print("mpy-cross 未導入(py -3.9 -m pip install --user mpy-cross)。同梱の .mpy を使います")
    if os.path.exists(MPY_OUT):
        return MPY_OUT
    sys.exit("kosu.mpy がありません。mpy-cross を入れてから再実行してください。")


def ask_token():
    while True:
        t1 = getpass.getpass("共有トークン (入力は表示されません): ")
        if not t1:
            print("  空です。入れ直してください。")
            continue
        if len(t1) < 6:
            print("  %d文字しかありません。getpass が効いていない可能性があります。" % len(t1))
            if input("  このまま使いますか? [y/N]: ").strip().lower() != "y":
                continue
        t2 = getpass.getpass("確認のためもう一度: ")
        if t1 != t2:
            print("  一致しません。入れ直してください。")
            continue
        print("  トークン %d文字を受け付けました。" % len(t1))
        return t1


# ----------------------------------------------------------- main
def main():
    do_app = "--config" not in sys.argv
    do_cfg = "--app" not in sys.argv

    url = token = None
    if do_cfg:
        print("=" * 58)
        print(" M5Dial 設定")
        print("=" * 58)
        url = "".join(input("GAS ウェブアプリの URL (/exec): ").split())
        if not url.endswith("/exec"):
            sys.exit("URL が /exec で終わっていません。テスト用の /dev ではなくデプロイの URL を使ってください。")
        print("  URL %d文字" % len(url))
        token = ask_token()

    mpy = build_mpy() if do_app else None

    s, port = connect()
    print("\n接続先ポート:", port)

    if do_app:
        raw_exec(s, "import os\ntry:\n    os.mkdir('/flash/libs')\nexcept Exception: pass")
        print("アプリを書き込み中 ...")
        if not put_file(s, DEV_MPY, open(mpy, "rb").read()):
            sys.exit("アプリの書き込みに失敗しました")
        if not put_file(s, DEV_MAIN, STUB):
            sys.exit("スタブの書き込みに失敗しました")

    if do_cfg:
        print("設定を書き込み中 ...")
        if not put_file(s, CFG_PATH, json.dumps({"url": url, "token": token}).encode("utf-8")):
            sys.exit("設定の書き込みに失敗しました")
        out, _ = raw_exec(s, "\n".join([
            "import json",
            "c = json.load(open(%r))" % CFG_PATH,
            "print('  url  :', len(c['url']), '文字  /exec:', c['url'].endswith('/exec'))",
            "print('  token:', len(c['token']), '文字')",
        ]))
        print(out)

    print("\n再起動します(ハードリセット) ...")
    s.write(b"\x02"); time.sleep(0.3)
    s.write(b"import machine; machine.reset()\r\n"); time.sleep(0.5)
    try:
        s.close()
    except Exception:
        pass

    dev = None
    for _ in range(40):
        time.sleep(0.4)
        c = [p for p in list_ports.comports() if p.vid == 0x303A]
        if c:
            dev = c[0]
            break
    if not dev:
        print("デバイスが戻ってきません。USB を挿し直してください。")
        return
    s = serial.Serial(); s.port = dev.device; s.baudrate = 115200; s.timeout = 0.3
    s.dtr = False; s.rts = False
    for _ in range(20):
        try:
            s.open(); break
        except Exception:
            time.sleep(0.4)
    buf = b""
    t0 = time.time()
    while time.time() - t0 < 45:
        buf += s.read(8192)
    s.close()
    print("-" * 58)
    sys.stdout.write(buf.decode("utf-8", "replace"))
    print("\n" + "-" * 58)
    print("完了。上の出力は Claude に貼って構いません(トークンは含まれません)。")


if __name__ == "__main__":
    main()
