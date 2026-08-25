"""
リアルタイム仮想運用（元手100万円 / 実際の値動き・FX主要JPYクロス）
------------------------------------------------------------
架空の資金で、いまの相場に対して自動売買を続ける常駐プログラム。
実際の売買・送金・入出金は一切行わない。投資助言でもない。

crypto_sim の live_trade.py と機構は同一（銘柄・コスト定数はすべて
data.py / broker.py / regime.py に一元化してあるため、ここに書き直す
必要はない）。FX固有の違いは1点だけ：土日は市場が休みなので、
現在値が取れない（fetch_liveが空dictを返す）ことがある。これは
既存の「現在値を取得できませんでした。次のループで再試行します」
という分岐がそのまま吸収する。

実行:
  python live_trade.py                 # 5分間隔で常駐（Ctrl+C で中断、状態は保存される）
  python live_trade.py --interval 60   # 間隔を変える（秒）
  python live_trade.py --once          # 1回だけ実行
  python live_trade.py --reset         # 元手100万円から仕切り直す
  python live_trade.py --status        # 現状だけ表示

やっていること（1ループごと）:
  1. 現在値を取得（5分足の最新・全銘柄まとめて1リクエスト）
  2. 相場付きを判定し、それに合わせて戦略とエクスポージャー上限を切り替える（regime.py）
  3. 保有中の銘柄はATRトレーリングストップを**毎ループ**判定する
     → 日足シグナルを待たずに、リアルタイムで損切りが効く
  4. 新規の建て玉判断は1日1回（日足が確定する単位）
  5. 目標ウェイトへリバランスし、状態とログを保存

停止条件:
  総資産が元金ゼロ（既定は0円、--stop-below で床を上げられる）まで落ちたら終了する。
  ※ 現物のみ・ショートなしなので、保有4通貨ペアすべてが無価値にならない限り
     0円には到達しない。実質的な撤退ラインを設けたい場合は --stop-below を明示する。

PCを落としている間は止まる。再開すると、その間の値動きは「飛んだ」ものとして
そのまま現在値から続行する（保有ポジションは持ち越し）。
"""

import argparse
import csv
import json
import os
import time
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd

import data as data_mod
import regime as regime_mod
from broker import Broker
from strategies.base import Context, atr
from strategies.trend import DonchianTrend, FilteredEqualWeight

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "state")
RESULTS_DIR = os.path.join(BASE_DIR, "results")
STATE_FILE = os.path.join(STATE_DIR, "live_state.json")
LOCK_FILE = os.path.join(STATE_DIR, "live.lock")
SNAPSHOT_FILE = os.path.join(STATE_DIR, "snapshot.json")
TRADES_LOG = os.path.join(RESULTS_DIR, "live_trades.csv")
EQUITY_LOG = os.path.join(RESULTS_DIR, "live_equity.csv")
ERROR_LOG = os.path.join(RESULTS_DIR, "live_errors.log")
SHADOW_LOG = os.path.join(RESULTS_DIR, "shadow_regime.csv")
MAX_ERRORS = 30            # 連続エラーがこの回数を超えたら諦めて終了

# シャドー判定（記録専用・実売買には一切使わない）のボラ閾値。
# sensitivity.py の実測で、2023-2026のCalmarが0.08だけ他の値（0.11〜0.25）から
# 明確に浮いている（0.69 vs 0.18〜0.22）ことが分かった——サンプル数が少ない中での
# 境界値なので、偶然か本物かを日々の判定の一致率で追跡する。
SHADOW_HIGH_VOL = 0.08

CAPITAL = 1_000_000        # 元手（円）
# 以下はすべて regime.py に一元化。実運用とバックテストが同じ値を同じ場所から読む
ATR_N = regime_mod.ATR_N
ATR_MULT = regime_mod.ATR_MULT           # トレーリングストップの幅（ATRの何倍か）
MAX_WEIGHT = regime_mod.MAX_WEIGHT       # 1銘柄あたりの上限
COOLDOWN_DAYS = regime_mod.COOLDOWN_DAYS # ストップ後に買い直さない日数
DEFAULT_INTERVAL = 300     # 5分

# regime.py が返す戦略キー -> 実際の戦略
PLAYBOOK = {
    "分散保有": lambda: FilteredEqualWeight(50),
    "ドンチャン20/10": lambda: DonchianTrend(entry=20, exit=10),
    "ドンチャン55/20": lambda: DonchianTrend(entry=55, exit=20),
}


ERROR_LOG_MAX_BYTES = 1_000_000     # これを超えたら1世代だけ退避して作り直す


def log_error(exc: Exception, count: int) -> None:
    """一時的な失敗を握りつぶさず、後から追えるようファイルに残す。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] ({count}回目) {type(exc).__name__}: {exc}"
    print(line)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    # 通信断が延々と続くとトレースバックで際限なく膨らむので上限を設ける
    try:
        if os.path.getsize(ERROR_LOG) > ERROR_LOG_MAX_BYTES:
            os.replace(ERROR_LOG, ERROR_LOG + ".1")
    except OSError:
        pass
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        traceback.print_exc(file=f)


def pid_alive(pid: int) -> bool:
    """
    そのPIDのプロセスが実在するか。
    Windows の os.kill(pid, 0) は「存在確認」ではなく TerminateProcess を呼ぶため、
    稼働中のインスタンスを殺してしまう。必ず OpenProcess で確認する。
    """
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def read_lock() -> dict | None:
    """
    稼働中の別インスタンスがいれば、そのロック情報を返す。
    2つ同時に走ると同じ state を奪い合って売買が矛盾するので、これで防ぐ。

    残骸のロックは無視する。判定は2段構え:
      - 心拍が古い（＝プロセスが応答していない）
      - PIDが実在しない（＝シャットダウンで消えた）
    心拍だけで見ると、PCを15分以内に再起動したときロックが「生きている」ように
    見えて自動起動が弾かれてしまう。PID の実在確認がその穴を塞ぐ。
    """
    if not os.path.exists(LOCK_FILE):
        return None
    try:
        with open(LOCK_FILE, encoding="utf-8") as f:
            lock = json.load(f)
        beat = datetime.strptime(lock["heartbeat"], "%Y-%m-%d %H:%M:%S")
        pid = int(lock["pid"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    stale_after = max(3 * int(lock.get("interval", DEFAULT_INTERVAL)), 120)
    if (datetime.now() - beat).total_seconds() > stale_after:
        return None
    if not pid_alive(pid):
        return None
    return lock


def write_lock(interval: int) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "interval": interval,
                   "heartbeat": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False)


def clear_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


class LiveTrader:
    def __init__(self, stop_below: float = 0.0):
        self.stop_below = stop_below
        self.panel = None
        self.panel_date = None
        os.makedirs(STATE_DIR, exist_ok=True)
        os.makedirs(RESULTS_DIR, exist_ok=True)
        self._load_state()

    # ---- 状態の保存・復元 ----
    def _load_state_from(self, path: str) -> bool:
        """
        指定ファイルから状態を復元できたら True。無い、または壊れていれば False。
        ファイルが存在しないだけ（初回起動・バックアップ未生成）では警告を出さない。
        """
        if not os.path.exists(path):
            return False
        try:
            with open(path, encoding="utf-8") as f:
                s = json.load(f)
            self.broker = Broker.from_dict(s["broker"])
            self.peaks = {k: float(v) for k, v in s.get("peaks", {}).items()}
            self.targets = {k: float(v) for k, v in s.get("targets", {}).items()}
            self.stopped = set(s.get("stopped", []))
            self.cooldown = dict(s.get("cooldown", {}))   # 銘柄 -> 再エントリー解禁日
            self.last_plan_date = s.get("last_plan_date")
            self.started = s.get("started")
            self.peak_equity = float(s.get("peak_equity", CAPITAL))
            self.stops = {}
            return True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # 壊れたファイルのまま次の判定に進まないと、CIでは次回も
            # 同じ壊れたファイルを .prev から復元して同じ場所で落ち続ける
            # （このメソッドは main() のリトライ機構の外側、__init__ から
            #  直接呼ばれるため、ここで拾わないと無言でプロセスごと落ちる）。
            print(f"[警告] 状態ファイルの読み込みに失敗しました: {exc}")
            print(f"  {path} が壊れている可能性があります。")
            return False

    def _load_state(self) -> None:
        if self._load_state_from(STATE_FILE):
            return
        # 直前の書き込みが壊れていても、1世代前（.bak）が残っていればそこから復旧を試みる。
        # .bak も無い／壊れていれば静かに False が返るだけ（二重に警告を出さない）。
        if self._load_state_from(STATE_FILE + ".bak"):
            print(f"  1世代前のバックアップ（{STATE_FILE}.bak）から復元しました。")
            return
        # 架空資金のシミュレーションなので、状態を失っても実害は
        # 「元手からやり直し」で済む。壊れたまま詰むよりましと判断する。
        print("  元手から再開します。")
        self.broker = Broker(cash=float(CAPITAL))
        self.peaks, self.targets, self.stopped = {}, {}, set()
        self.cooldown = {}
        self.last_plan_date = None
        self.started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.peak_equity = float(CAPITAL)
        self.stops = {}     # 銘柄 -> 現在のストップ価格（表示用・毎ループ再計算）

    def _save_state(self) -> None:
        s = {
            "broker": self.broker.to_dict(),
            "peaks": self.peaks,
            "targets": self.targets,
            "stopped": sorted(self.stopped),
            "cooldown": self.cooldown,
            "last_plan_date": self.last_plan_date,
            "started": self.started,
            "peak_equity": self.peak_equity,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        # 直前の世代を1つだけ.bakとして残す。今回の書き込み内容が何らかの理由で
        # 壊れていても（想定外の値・将来のバグ等）、1つ前の状態に戻れる余地を残す。
        # os.replaceで運んでいる限り書き込み中の異常終了では壊れないが、
        # それとは別の「書けた内容そのものが壊れている」場合への備え。
        if os.path.exists(STATE_FILE):
            try:
                os.replace(STATE_FILE, STATE_FILE + ".bak")
            except OSError:
                pass
        os.replace(tmp, STATE_FILE)   # 書き込み中の異常終了で状態を壊さない

    # ---- データ ----
    def _refresh_daily(self) -> None:
        """
        日足パネルは日付が変わったときだけ取り直す。

        判定はUTCの日付で行う（_panel_now と同じ理由）。ここをJSTのままにすると、
        UTCで新しい日足が確定してから次のJST 0時までの最大15時間、
        self.panel が前日のまま古くなる。その間 _panel_now は「前日の終値を
        ベースにした合成行」を作り続けることになり、55日高値やATRが
        実際の値動きより遅れて反映される。
        """
        today = datetime.now(timezone.utc).date()
        if self.panel is not None and self.panel_date == today:
            return
        self.panel = data_mod.load_panel(force=self.panel is not None)
        self.panel_date = today

    def _panel_now(self, prices: dict) -> dict:
        """
        日足パネルの最終行を現在値で上書きした、いま時点のパネル。

        「今日の足」の判定にはUTCの日付を使う。FXの日足もUTC区切りで、
        ローカル日付で判定すると日本時間の00:00〜09:00のあいだ1日ずれ、
        実データの上に合成行が1本余計に積まれてしまう。そうなると前日比が
        「数分前との比較」になり、移動平均・ATR・ドンチャンの集計窓も1本ずれる。
        記録の時刻は日本時間のままでよいが、足の同定だけはデータ側の暦に合わせる。

        土日は fetch_live() が空 dict を返すため、そもそも step() の
        この手前で次ループへ回っており、ここが土日に呼ばれることは無い。
        """
        panel = {k: v.copy() for k, v in self.panel.items()}
        today = pd.Timestamp(datetime.now(timezone.utc).date())
        if panel["close"].index[-1] != today:
            for f in panel:
                panel[f].loc[today] = panel[f].iloc[-1]
        for sym, p in prices.items():
            if sym not in panel["close"].columns:
                continue
            panel["close"].loc[today, sym] = p
            hi, lo = panel["high"].loc[today, sym], panel["low"].loc[today, sym]
            panel["high"].loc[today, sym] = p if hi != hi else max(float(hi), p)
            panel["low"].loc[today, sym] = p if lo != lo else min(float(lo), p)
        return panel

    # ---- 判断 ----
    def _plan(self, panel: dict, reg: dict, equity: float) -> dict:
        """1日1回、その日の目標ウェイトを決める。"""
        strat = PLAYBOOK[reg["戦略"]]()
        if isinstance(strat, DonchianTrend):
            strat.state = {s: {"peak": p} for s, p in self.peaks.items()}
        ctx = Context(panel, len(panel["close"].index) - 1, self.broker, equity)
        raw = strat.targets(ctx)
        if isinstance(strat, DonchianTrend):
            self.peaks = {s: v["peak"] for s, v in strat.state.items()}

        # ストップで切った直後の銘柄は買い直さない（往復売買を避ける）
        # 起点(_guardでの記録)と同じUTC基準で比較する
        today = str(datetime.now(timezone.utc).date())
        self.cooldown = {s: d for s, d in self.cooldown.items() if d > today}
        raw = {s: w for s, w in raw.items() if s not in self.cooldown}

        raw = {s: min(w, MAX_WEIGHT) for s, w in raw.items()}   # 1銘柄への集中を抑える
        total = sum(raw.values())
        cap = reg["上限"]
        if total > cap and total > 0:                # レジームごとの上限に収める
            raw = {s: w * cap / total for s, w in raw.items()}
        # 損切りした銘柄は、新しいシグナルが出たら再エントリーを許可する
        self.stopped = {s for s in self.stopped if s not in raw}
        return raw

    def _guard(self, panel: dict, prices: dict) -> dict:
        """
        毎ループのトレーリングストップ判定。現在値で見て、
        「建玉後の最高値からATR×ATR_MULT」下げた銘柄は目標ウェイトを0にする。
        """
        eff = dict(self.targets)
        highs, lows, closes = panel["high"], panel["low"], panel["close"]
        for sym in list(self.broker.positions):
            p = prices.get(sym)
            if p is None:
                continue
            self.peaks[sym] = max(self.peaks.get(sym, p), p)
            c, h, l = closes[sym].dropna(), highs[sym].dropna(), lows[sym].dropna()
            a = atr(h, l, c, ATR_N)
            if a != a or a <= 0:
                continue
            stop = self.peaks[sym] - ATR_MULT * a
            self.stops[sym] = stop
            if p <= stop:
                eff[sym] = 0.0
                self.stopped.add(sym)
                # 解禁日を記録。ここが唯一クールダウンを開始する場所（UTC基準で統一）
                until = datetime.now(timezone.utc).date() + timedelta(days=COOLDOWN_DAYS)
                self.cooldown[sym] = str(until)
        for sym in self.stopped:
            eff[sym] = 0.0
        return eff

    def _signal_level(self, reg: dict, sym: str, panel: dict):
        """
        いまの戦略における「買いシグナルが出る水準」。戦略ごとに違うので、
        ラベルと水準の両方を返す。ダッシュボードで「あと何%で点灯するか」を出すため。
        """
        closes, highs = panel["close"], panel["high"]
        c, h = closes[sym].dropna(), highs[sym].dropna()
        key = reg["戦略"]
        if key.startswith("ドンチャン"):
            entry = 55 if "55" in key else 20
            if len(h) < entry + 1:
                return None, None
            return f"{entry}日高値", float(h.iloc[-(entry + 1):-1].max())
        if len(c) < 50:
            return None, None
        return "50日線", float(c.iloc[-50:].mean())      # 分散保有

    def _universe(self, reg: dict, prices: dict, panel: dict) -> list:
        closes, highs, lows = panel["close"], panel["high"], panel["low"]
        out = []
        for sym in closes.columns:
            c = closes[sym].dropna()
            if len(c) < 2:
                continue
            h, l = highs[sym].dropna(), lows[sym].dropna()
            price = prices.get(sym) or float(c.iloc[-1])
            prev = float(c.iloc[-2])
            ma50 = float(c.iloc[-50:].mean()) if len(c) >= 50 else None
            ma200 = float(c.iloc[-200:].mean()) if len(c) >= 200 else None
            label, level = self._signal_level(reg, sym, panel)
            a = atr(h, l, c, ATR_N)
            out.append({
                "symbol": sym,
                "name": data_mod.SYMBOLS.get(sym, sym),
                "price": price,
                "change_pct": (price / prev - 1) * 100 if prev else None,
                "ma50": ma50,
                "ma200": ma200,
                "vs_ma50_pct": (price / ma50 - 1) * 100 if ma50 else None,
                "vs_ma200_pct": (price / ma200 - 1) * 100 if ma200 else None,
                "signal_label": label,
                "signal_level": level,
                "signal_gap_pct": (level / price - 1) * 100 if level else None,
                "atr": a if a == a else None,
                "held": sym in self.broker.positions,
                "stop": self.stops.get(sym) if sym in self.broker.positions else None,
                "cooldown_until": self.cooldown.get(sym),
                # 一覧にスパークラインを描くための直近30日の終値。
                # 数字の羅列より「どういう形で下げているか」が一目で分かる。
                "spark": [float(v) for v in c.iloc[-30:]],
            })
        # シグナルに近いものを上に
        out.sort(key=lambda r: (not r["held"],
                                r["signal_gap_pct"] if r["signal_gap_pct"] is not None else 1e9))
        return out

    def _save_snapshot(self, ts: str, reg: dict, prices: dict,
                       invested: float, equity: float, panel: dict) -> None:
        """
        ブラウザ表示用の現況スナップショット。dashboard.py はこれを読むだけなので、
        画面を開くたびに価格を取りにいく必要がなく、表示も速い。
        """
        positions = []
        for sym, pos in self.broker.positions.items():
            p = prices.get(sym)
            value = pos.qty * p if p else pos.cost_jpy
            positions.append({
                "symbol": sym,
                "name": data_mod.SYMBOLS.get(sym, sym),
                "qty": pos.qty,
                "price": p,
                "cost": pos.cost_jpy,
                "value": value,
                "upnl": value - pos.cost_jpy,
                "upnl_pct": (value / pos.cost_jpy - 1) * 100 if pos.cost_jpy else 0.0,
                "weight": value / equity * 100 if equity else 0.0,
                "stop": self.stops.get(sym),
            })
        snap = {
            "ts": ts,
            "pid": os.getpid(),
            "started": self.started,
            "capital": CAPITAL,
            "cash": self.broker.cash,
            "invested": invested,
            "equity": equity,
            "pnl": equity - CAPITAL,
            "pnl_pct": (equity / CAPITAL - 1) * 100,
            "peak_equity": self.peak_equity,
            "drawdown_pct": (equity / self.peak_equity - 1) * 100 if self.peak_equity else 0.0,
            "regime": reg["レジーム"],
            "strategy": reg["戦略"],
            "cap_pct": reg["上限"] * 100,
            "reason": reg["理由"],
            # レジーム判定の各条件が、切り替わる閾値までどれだけ離れているか。
            # 監視ユニバースの「点灯まで」バーと同じ発想で、相場付きが変わる予兆に
            # 気づけるようにする。すべて regime.py の値をそのまま使う（ここで計算し直さない）。
            "trend_pct": reg["200日線比"] * 100 if reg["200日線比"] == reg["200日線比"] else None,
            "vol_pct": reg["年率ボラ"] * 100 if reg["年率ボラ"] == reg["年率ボラ"] else None,
            "vol_high_pct": regime_mod.HIGH_VOL * 100,
            "breadth_pct": reg["上昇銘柄比率"] * 100,
            "breadth_min_pct": regime_mod.BREADTH_MIN * 100,
            "positions": positions,
            "universe": self._universe(reg, prices, panel),
            "data_gaps": data_mod.find_gaps(panel, days=30),
            "data_gap_retried_at": data_mod.last_gap_retry(),
            "targets": self.targets,
            "prices": prices,
        }
        tmp = SNAPSHOT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SNAPSHOT_FILE)

    # ---- ログ ----
    def _log_trades(self, before: int) -> list:
        new = self.broker.trades[before:]
        if not new:
            return []
        is_new_file = not os.path.exists(TRADES_LOG)
        with open(TRADES_LOG, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if is_new_file:
                w.writerow(["日時", "銘柄", "売買", "数量", "約定単価",
                            "金額(円)", "実現損益(円)", "理由"])
            for t in new:
                w.writerow([t.date, t.symbol, t.side, f"{t.qty:.4f}",
                            f"{t.price:.3f}", f"{t.amount_jpy:.0f}",
                            f"{t.realized_jpy:.0f}" if t.side == "売" else "", t.reason])
        return new

    def _log_shadow_regime(self, panel: dict, reg: dict) -> None:
        """
        実売買には一切影響しない記録専用の処理。別のボラ閾値（SHADOW_HIGH_VOL）で
        同じ判定をもう一度行い、実際の判定と一致するかどうかだけをログに残す。
        ここで計算した値は self.targets / self.broker のどちらにも渡さない。
        1日1回、建玉方針を練り直すタイミング（_plan と同じ頻度）でのみ呼ぶ。
        """
        try:
            shadow = regime_mod.classify(panel["close"], high_vol=SHADOW_HIGH_VOL)
        except Exception as exc:
            # 記録用のおまけなので、失敗しても本編（実際の売買判断）には影響させない
            log_error(exc, 0)
            return
        is_new_file = not os.path.exists(SHADOW_LOG)
        with open(SHADOW_LOG, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if is_new_file:
                w.writerow(["日時", "実運用レジーム", f"シャドー(ボラ閾値{SHADOW_HIGH_VOL:.2f})", "一致"])
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        reg["レジーム"], shadow["レジーム"],
                        "○" if reg["レジーム"] == shadow["レジーム"] else "×"])

    def _log_equity(self, ts: str, reg: dict, cash: float, invested: float,
                    equity: float) -> None:
        is_new_file = not os.path.exists(EQUITY_LOG)
        with open(EQUITY_LOG, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if is_new_file:
                w.writerow(["日時", "レジーム", "戦略", "現金(円)", "評価額(円)",
                            "総資産(円)", "損益(円)", "損益率(%)", "建玉率(%)"])
            w.writerow([ts, reg["レジーム"], reg["戦略"], f"{cash:.0f}",
                        f"{invested:.0f}", f"{equity:.0f}",
                        f"{equity - CAPITAL:.0f}",
                        f"{(equity / CAPITAL - 1) * 100:.2f}",
                        f"{invested / equity * 100:.1f}" if equity > 0 else "0.0"])

    # ---- 1ループ ----
    def step(self, verbose: bool = True) -> bool:
        """1回ぶんの処理。運用を続けてよければ True、停止条件に触れたら False。"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._refresh_daily()

        prices = data_mod.fetch_live()
        if not prices:
            # FXは土日休場のため、週末はここに毎回来る（想定内）
            print(f"[{ts}] 現在値を取得できませんでした（休場中の可能性）。次のループで再試行します。")
            return True

        panel = self._panel_now(prices)
        equity = self.broker.equity(prices)
        reg = regime_mod.classify(panel["close"])

        # 新しい日になったら建て玉方針を練り直す。判定はUTC基準
        # （日足がUTC区切りのため。JSTのままだと確定前のデータで練り直したり、
        #  確定後も最大15時間気づかなかったりする）
        today = str(datetime.now(timezone.utc).date())
        replanned = self.last_plan_date != today
        if replanned:
            self.targets = self._plan(panel, reg, equity)
            self.last_plan_date = today
            self._log_shadow_regime(panel, reg)

        eff = self._guard(panel, prices)

        n_before = len(self.broker.trades)
        self.broker.rebalance(ts, prices, eff,
                              reason=f"{reg['レジーム']}/{reg['戦略']}")
        new_trades = self._log_trades(n_before)

        # 決済済み銘柄のピーク値を捨てる。残すと再エントリー直後に
        # 古い高値を基準にしたストップへ即座に引っかかる
        self.peaks = {s: v for s, v in self.peaks.items() if s in self.broker.positions}

        invested = self.broker.position_value(prices)
        equity = self.broker.cash + invested
        self.peak_equity = max(self.peak_equity, equity)
        self._log_equity(ts, reg, self.broker.cash, invested, equity)
        self._save_state()
        self._save_snapshot(ts, reg, prices, invested, equity, panel)

        if verbose:
            self._print(ts, reg, prices, invested, equity, new_trades, replanned)

        if equity <= self.stop_below:
            print(f"\n停止条件に到達しました（総資産 {equity:,.0f}円 ≦ "
                  f"{self.stop_below:,.0f}円）。運用を終了します。")
            return False
        return True

    def _print(self, ts, reg, prices, invested, equity, new_trades, replanned) -> None:
        pnl = equity - CAPITAL
        dd = equity / self.peak_equity - 1.0
        print(f"\n[{ts}]  {reg['レジーム']} → {reg['戦略']}（上限{reg['上限']*100:.0f}%）"
              + ("  ※方針を練り直しました" if replanned else ""))
        print(f"  根拠: {reg['理由']}")
        for t in new_trades:
            mark = "＋" if t.side == "買" else "－"
            extra = (f"  実現損益 {t.realized_jpy:+,.0f}円" if t.side == "売" else "")
            print(f"  {mark}{t.side} {t.symbol}  {t.amount_jpy:,.0f}円 "
                  f"@ {t.price:,.3f}{extra}  [{t.reason}]")
        print(f"  現金 {self.broker.cash:,.0f}円 / 評価額 {invested:,.0f}円 "
              f"→ 総資産 {equity:,.0f}円（{pnl:+,.0f}円 / {pnl/CAPITAL*100:+.2f}%）"
              f"  ピーク比 {dd*100:.1f}%")
        if self.broker.positions:
            for sym, pos in self.broker.positions.items():
                p = prices.get(sym)
                val = pos.qty * p if p else pos.cost_jpy
                upnl = val - pos.cost_jpy
                print(f"    {sym:<9} 評価 {val:>10,.0f}円  含み {upnl:+,.0f}円"
                      f"  ({val/equity*100:4.1f}%)")
        else:
            print("    保有なし（現金100%）")

    def status(self) -> None:
        """
        トレーダーが書いたスナップショットを読むだけ。通信しない。
        確認したいだけなのに価格を取りに行くと、遅いうえ回線が無いと失敗する。
        """
        snap = None
        if os.path.exists(SNAPSHOT_FILE):
            try:
                with open(SNAPSHOT_FILE, encoding="utf-8") as f:
                    snap = json.load(f)
            except (OSError, json.JSONDecodeError):
                snap = None

        lock = read_lock()
        print(f"稼働状況: {'稼働中 (PID %d)' % lock['pid'] if lock else '停止中'}")
        print(f"運用開始: {self.started}")

        if snap is None:
            print("まだスナップショットがありません（トレーダーの初回ループ前）。")
            print(f"現金 {self.broker.cash:,.0f}円")
            return

        print(f"データ時刻: {snap['ts']}")
        print(f"相場判断: {snap['regime']} → {snap['strategy']}（上限{snap['cap_pct']:.0f}%）")
        print(f"  {snap['reason']}")
        print(f"現金 {snap['cash']:,.0f}円 / 評価額 {snap['invested']:,.0f}円 "
              f"→ 総資産 {snap['equity']:,.0f}円（{snap['pnl']:+,.0f}円 / {snap['pnl_pct']:+.2f}%）")
        if snap["positions"]:
            for p in snap["positions"]:
                print(f"  {p['name']:<10} 評価 {p['value']:>10,.0f}円  "
                      f"含み {p['upnl']:+,.0f}円 ({p['upnl_pct']:+.2f}%)"
                      + (f"  ストップ {p['stop']:,.3f}" if p.get("stop") else ""))
        else:
            print("  保有なし（現金100%）")
        if snap.get("data_gaps"):
            print(f"  [警告] 日足データの欠損: {', '.join(snap['data_gaps'])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"実行間隔（秒）。既定 {DEFAULT_INTERVAL}")
    ap.add_argument("--once", action="store_true", help="1回だけ実行して終了")
    ap.add_argument("--status", action="store_true", help="現状を表示して終了")
    ap.add_argument("--reset", action="store_true", help="元手100万円から仕切り直す")
    ap.add_argument("--stop-below", type=float, default=0.0,
                    help="総資産がこの額以下になったら終了（既定0＝元金ゼロ）")
    ap.add_argument("--force", action="store_true",
                    help="他インスタンスが稼働中でも強制的に起動する")
    ap.add_argument("--ci", action="store_true",
                    help="CI用。1回だけ実行し、ロックを作らない（実行環境が毎回変わるため）")
    args = ap.parse_args()

    if args.ci:
        args.once = True
        args.force = True     # 前回のロックは別マシンのもの。参照しない

    if not args.status and not args.force:
        lock = read_lock()
        if lock:
            print(f"すでに別のインスタンスが稼働中です（PID {lock['pid']} / "
                  f"最終更新 {lock['heartbeat']}）。")
            print("2つ同時に走らせると同じ状態ファイルを奪い合い、売買が矛盾します。")
            print("そちらを閉じてから起動するか、状態確認だけなら "
                  "`python live_trade.py --status` を使ってください。")
            return

    if args.reset and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        print("状態をリセットしました（元手100万円から再開）")

    trader = LiveTrader(stop_below=args.stop_below)

    if args.status:
        trader.status()
        return

    print("=" * 78)
    print("リアルタイム仮想運用  元手 1,000,000円（FX・主要JPYクロス）")
    print("架空の資金によるシミュレーションです。実際の売買・送金は一切行いません。")
    print("コスト: スプレッド相当0.01%（概算） / 現物のみ（レバレッジなし）"
          + ("" if args.once else f" / 実行間隔 {args.interval}秒"))
    print(f"停止ライン: 総資産 {args.stop_below:,.0f}円 以下")
    if args.ci:
        print("CIモード: 1回だけ実行して終了します（ロックは作りません）")
    elif not args.once:
        print("Ctrl+C で中断できます（状態は保存され、次回そこから再開します）")
    print("=" * 78)

    errors = 0
    try:
        while True:
            if not args.ci:
                # 心拍。止まれば他インスタンスが引き継げる。
                # CIでは実行環境が毎回変わるので、残すと次回の判定を誤らせる
                write_lock(args.interval)
            try:
                keep_going = trader.step()
                errors = 0
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # 無人で走らせる前提なので、通信断などの一時的な失敗で
                # ループごと落とさない。連続で失敗し続けたときだけ諦める。
                errors += 1
                keep_going = errors < MAX_ERRORS
                log_error(exc, errors)
                if not keep_going:
                    print(f"エラーが{MAX_ERRORS}回続いたため終了します。")
            if not keep_going or args.once:
                break
            # 失敗が続くときは間隔を空けて再試行する
            time.sleep(args.interval * min(errors + 1, 6) if errors else args.interval)
    except KeyboardInterrupt:
        trader._save_state()
        print("\n中断しました。状態は保存済みです。"
              "`python live_trade.py` で同じところから再開できます。")
    finally:
        if not args.ci:
            clear_lock()
    if args.ci and errors:
        raise SystemExit(1)      # 失敗はワークフロー上で赤く見えるようにする


if __name__ == "__main__":
    main()
