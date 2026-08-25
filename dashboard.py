"""
運用状況をブラウザで見るためのローカルサーバ
------------------------------------------------------------
実行:
  python dashboard.py            → http://127.0.0.1:8787 をブラウザで開く
  python dashboard.py --port 9000
  python dashboard.py --open     → 既定のブラウザで自動的に開く

live_trade.py とは別プロセスで動く。トレーダーが止まっていても画面は開けて、
「停止中」と分かるようにするため（組み込みにすると、止まった瞬間に確認手段も消える）。
表示するのはトレーダーが書いたファイルだけなので、この画面が価格を取りにいくことはない。

  state/snapshot.json   … 現況（毎ループ更新）
  state/live.lock       … 稼働判定に使う心拍
  results/live_equity.csv … 資産推移
  results/live_trades.csv … 売買履歴

127.0.0.1 のみで待ち受ける（LANや外部からは見えない）。
"""

import argparse
import csv
import json
import os
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import live_trade as lt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(BASE_DIR, "dashboard.html")
MAX_CURVE_POINTS = 1500     # これを超えたら間引いて返す
MAX_TRADES = 40

# CSVダウンロードの対象をホワイトリストで固定する（任意のファイルを晒さないため）。
# 静的配信（publish_static.py）側は results/ をそのまま同梱して同じファイルへの
# 相対リンクで代替しており、こちらのエンドポイントは使わない。
DOWNLOADS = {
    "/download/live_equity.csv": (lambda: lt.EQUITY_LOG, "資産推移.csv"),
    "/download/live_trades.csv": (lambda: lt.TRADES_LOG, "売買履歴.csv"),
}


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


_csv_cache: dict[str, tuple] = {}


def _read_csv(path: str) -> list[dict]:
    """
    更新時刻とサイズが変わっていなければ前回の結果を使い回す。
    資産ログは5分ごとに1行増えるだけなのに、画面は15秒ごとに問い合わせてくる。
    毎回ファイル全体を読み直すと、運用が長引くほど無駄が増えていく。
    """
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    hit = _csv_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    _csv_cache[path] = (key, rows)
    return rows


def _equity_curve() -> list[dict]:
    rows = _read_csv(lt.EQUITY_LOG)

    # ピーク比（ドローダウン）は間引き前の全行から計算する。増減(delta)は隣接点の
    # 差を合算すれば間引きで消えた区間ぶんも正しく復元できるが、ピークはそうはいかない。
    # 間引き後の代表点だけで最高値を追うと、間引きで消えた区間にあった本当の
    # ピークを見逃し、ドローダウンを実際より浅く見せてしまう。
    parsed = []
    peak = None
    for r in rows:
        try:
            equity = float(r["総資産(円)"])
        except (KeyError, ValueError):
            continue
        peak = equity if peak is None else max(peak, equity)
        dd_pct = (equity / peak - 1) * 100 if peak else 0.0
        try:
            expo_pct = float(r["建玉率(%)"])
        except (KeyError, ValueError):
            expo_pct = None
        parsed.append({"t": r["日時"], "equity": equity,
                       "regime": r.get("レジーム", ""), "dd_pct": dd_pct,
                       "expo_pct": expo_pct})

    if len(parsed) > MAX_CURVE_POINTS:    # 古いほど粗くてよい
        step = len(parsed) // MAX_CURVE_POINTS + 1
        thinned = parsed[::step]
        # (len-1) が step で割り切れると最終行がすでに含まれており、
        # 単純に + parsed[-1:] すると同じ時刻・同じ値の点が2つ並んでしまう
        if thinned[-1] is not parsed[-1]:
            thinned = thinned + parsed[-1:]
        parsed = thinned

    # 前レコードからの増減。間引き後の隣接点同士の差になるので、
    # 間引きで消えた区間ぶんも合算した「正味の変化」を表す（それで正しい）。
    # 3（日次集約）を実装する際は、ここに集約単位を切り替える分岐を足す。
    prev_equity = None
    for p in parsed:
        p["delta"] = None if prev_equity is None else p["equity"] - prev_equity
        prev_equity = p["equity"]
    return parsed


def _trades() -> list[dict]:
    rows = _read_csv(lt.TRADES_LOG)[-MAX_TRADES:]
    out = []
    for r in reversed(rows):              # 新しいものを先に
        try:
            out.append({
                "t": r["日時"], "symbol": r["銘柄"], "side": r["売買"],
                "price": float(r["約定単価"]), "amount": float(r["金額(円)"]),
                "realized": float(r["実現損益(円)"]) if r.get("実現損益(円)") else None,
                "reason": r.get("理由", ""),
            })
        except (KeyError, ValueError):
            continue
    return out


_HOLD_LABELS = ["0〜1日", "2〜5日", "6〜10日", "11〜20日", "21日以上"]


def _hold_bucket(days: float) -> str:
    if days <= 1:
        return _HOLD_LABELS[0]
    if days <= 5:
        return _HOLD_LABELS[1]
    if days <= 10:
        return _HOLD_LABELS[2]
    if days <= 20:
        return _HOLD_LABELS[3]
    return _HOLD_LABELS[4]


def _holding_periods(rows: list[dict]) -> dict:
    """
    銘柄ごとに「無し→有り」に転じた最初の買いを建玉日、
    「有り→無し」に戻った売りを手仕舞い日とみなして保有日数を求める。
    broker.py はポジションを銘柄単位で合算管理しており個別ロットを
    区別しないため、この定義は実際の管理単位と一致する
    （途中で買い増しても、建玉日は最初の買いのまま動かさない）。
    """
    qty: dict[str, float] = {}
    entry: dict[str, str] = {}
    days_list: list[float] = []
    for r in rows:
        try:
            sym, side, q, ts = r["銘柄"], r["売買"], float(r["数量"]), r["日時"]
        except (KeyError, ValueError):
            continue
        before = qty.get(sym, 0.0)
        if side == "買":
            if before <= 1e-12:
                entry[sym] = ts
            qty[sym] = before + q
        elif side == "売":
            after = before - q
            qty[sym] = after
            if after <= 1e-12 and sym in entry:
                try:
                    d0 = datetime.strptime(entry[sym], "%Y-%m-%d %H:%M:%S")
                    d1 = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                    days_list.append((d1 - d0).total_seconds() / 86400)
                except ValueError:
                    pass
                del entry[sym]
    if not days_list:
        return {"count": 0, "buckets": []}
    counts = {label: 0 for label in _HOLD_LABELS}
    for d in days_list:
        counts[_hold_bucket(d)] += 1
    days_list.sort()
    return {
        "count": len(days_list),
        "median_days": days_list[len(days_list) // 2],
        "buckets": [{"label": label, "count": counts[label]} for label in _HOLD_LABELS],
    }


def _trade_stats() -> dict:
    """
    決済済みの売買だけを集計する。含み損益は snapshot 側にあるので、
    ここでは「確定した結果」だけを見る。運用が長引くほど、
    今の評価額より「これまで勝てているか」が判断材料になる。
    """
    rows = _read_csv(lt.TRADES_LOG)
    holding = _holding_periods(rows)
    realized = []
    by_symbol: dict[str, dict] = {}
    for r in rows:
        if r.get("売買") != "売" or not r.get("実現損益(円)"):
            continue
        try:
            v = float(str(r["実現損益(円)"]).replace(",", ""))
        except ValueError:
            continue
        realized.append(v)
        # 銘柄ごとの実績。どの通貨で勝てているかは全体の合計では分からない
        s = by_symbol.setdefault(r.get("銘柄", "?"),
                                 {"symbol": r.get("銘柄", "?"), "realized": 0.0,
                                  "count": 0, "wins": 0})
        s["realized"] += v
        s["count"] += 1
        s["wins"] += 1 if v > 0 else 0
    if not realized:
        return {"count": 0, "by_symbol": [], "holding": holding}
    wins = [v for v in realized if v > 0]
    losses = [v for v in realized if v <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "count": len(realized),
        "wins": len(wins),
        "win_rate": len(wins) / len(realized) * 100,
        "total": sum(realized),
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "best": max(realized),
        "worst": min(realized),
        "by_symbol": sorted(by_symbol.values(), key=lambda s: -s["realized"]),
        "holding": holding,
    }


def _recent_errors(limit: int = 5) -> list[str]:
    """
    直近のエラーを画面に出すために読む。
    「ログを確認してください」と書きながらブラウザからは見られない、では
    診断の役に立たない。行頭が [日時] の行だけを拾う（本文は要約のみ）。
    """
    try:
        with open(lt.ERROR_LOG, encoding="utf-8") as f:
            lines = [l.rstrip() for l in f if l.startswith("[")]
    except OSError:
        return []
    return lines[-limit:][::-1]


def _error_frequency(days: int = 14) -> list[dict]:
    """
    直近days日分、日ごとのエラー発生件数。直近5件（_recent_errors）だけでは
    「頻度が増えている」という傾向には気づけない。1〜2回の通信断はノイズだが、
    発生頻度そのものが増えているのは配信元や実行環境の劣化サイン。
    """
    try:
        with open(lt.ERROR_LOG, encoding="utf-8") as f:
            lines = [l for l in f if l.startswith("[")]
    except OSError:
        lines = []
    counts: dict[str, int] = {}
    for line in lines:
        # 形式は "[YYYY-MM-DD HH:MM:SS] (n回目) ..."（log_error参照）
        day = line[1:11]
        counts[day] = counts.get(day, 0) + 1
    today = datetime.now().date()
    return [{"date": str(today - timedelta(days=i)), "count": counts.get(str(today - timedelta(days=i)), 0)}
            for i in range(days - 1, -1, -1)]


def build_state() -> dict:
    snap = _read_json(lt.SNAPSHOT_FILE)
    lock = lt.read_lock()

    running = lock is not None
    stale_reason = ""
    if not running:
        raw = _read_json(lt.LOCK_FILE)
        if raw:
            stale_reason = f"最後の心拍 {raw.get('heartbeat', '不明')}"

    return {
        "running": running,
        "pid": lock.get("pid") if lock else None,
        "interval": lock.get("interval") if lock else None,
        "heartbeat": lock.get("heartbeat") if lock else None,
        "stale_reason": stale_reason,
        "snapshot": snap,
        "curve": _equity_curve(),
        "trades": _trades(),
        "stats": _trade_stats(),
        "errors": _recent_errors(),
        "error_freq": _error_frequency(),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


class Server(ThreadingHTTPServer):
    # Windows では SO_REUSEADDR が有効だと「使用中のポート」にも bind が通ってしまい、
    # 二重起動が検出できないまま後から起動した方がポートを奪う。明示的に切る。
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, content_type: str, extra_headers: dict | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(PAGE_FILE, "rb") as f:
                    self._send(f.read(), "text/html; charset=utf-8")
            except OSError:
                self.send_error(500, "dashboard.html が見つかりません")
        elif path == "/api/state":
            body = json.dumps(build_state(), ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
        elif path in DOWNLOADS:
            src_getter, name = DOWNLOADS[path]
            try:
                with open(src_getter(), "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(404, "まだ記録がありません")
                return
            # ファイル名に日本語を含むので RFC 6266 の filename* で指定する
            self._send(body, "text/csv; charset=utf-8",
                       {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
        else:
            self.send_error(404)

    def log_message(self, *args) -> None:
        pass      # アクセスログは出さない（コンソールが埋まるため）


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--open", action="store_true", help="ブラウザを自動で開く")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    try:
        server = Server(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        # 二重起動や他アプリとのポート衝突。生の例外を出しても分かりにくいので言い換える。
        print(f"ポート {args.port} を使えませんでした: {exc}")
        print(f"すでにダッシュボードが起動している可能性があります。まず {url} を開いてみてください。")
        print(f"別のポートで動かすなら: python dashboard.py --port 8788")
        return
    print(f"運用状況ダッシュボード: {url}")
    print("Ctrl+C で終了します。（このサーバは表示専用で、売買には一切関与しません）")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
