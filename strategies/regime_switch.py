"""
レジーム切替戦略
------------------------------------------------------------
crypto_sim の strategies/regime_switch.py と機構は同一。相場付き
（regime.classify()）を判定してから戦略とエクスポージャー上限を選ぶ、
実運用候補の本命戦略。

crypto_sim では live_trade.py（リアルタイム運用）と全く同じ判断を
ここで再現し、バックテストで検証できるようにしていた。fx_sim は
今のところ検証基盤（バックテスト・パラメータ感度）のみで live_trade.py
相当のリアルタイム運用は未実装。将来追加する場合は、ここと同じ
regime.py の値（MAX_WEIGHT・COOLDOWN_DAYS・ATR_N・ATR_MULT）を
そのまま読ませ、実運用とバックテストで前提がズレないようにすること。
"""

import regime as regime_mod

from .base import Strategy, Context, atr
from .trend import DonchianTrend, FilteredEqualWeight

# 以下はすべて regime.py に一元化。将来リアルタイム運用を作る場合も
# 同じ値を同じ場所から読ませること（ここに数値を書き直さない）。
MAX_WEIGHT = regime_mod.MAX_WEIGHT
ATR_N = regime_mod.ATR_N
ATR_MULT = regime_mod.ATR_MULT


class RegimeSwitching(Strategy):
    name = "レジーム切替"

    def __init__(self, max_weight: float = MAX_WEIGHT,
                 atr_mult: float = ATR_MULT, use_stop: bool = True,
                 cooldown: int = regime_mod.COOLDOWN_DAYS):
        self.max_weight = max_weight
        self.atr_mult = atr_mult
        self.use_stop = use_stop
        self.cooldown = cooldown      # ストップ後、この本数ぶん再エントリーを禁じる
        self.cool_until = {}
        self.subs = {
            # ATRパラメータを渡さないと DonchianTrend 自身の既定値(14, 3.0)を使う
            # 独立した経路になり、regime.py の値と気づかないうちにズレる
            "分散保有": FilteredEqualWeight(50),
            "ドンチャン20/10": DonchianTrend(entry=20, exit=10,
                                            atr_n=regime_mod.ATR_N, atr_mult=regime_mod.ATR_MULT),
            "ドンチャン55/20": DonchianTrend(entry=55, exit=20,
                                            atr_n=regime_mod.ATR_N, atr_mult=regime_mod.ATR_MULT),
        }
        self.warmup = regime_mod.TREND_MA + 2      # 200日線が必要
        self.peaks = {}          # 銘柄 -> 建玉後の最高終値
        self.last_regime = None

    def targets(self, ctx: Context) -> dict:
        if ctx.i < self.warmup:
            return {}
        closes = ctx.hist("close")
        reg = regime_mod.classify(closes)
        self.last_regime = reg["レジーム"]

        sub = self.subs[reg["戦略"]]
        # ドンチャン系は自分でピークを持つので、こちらの記録と同期させる
        if isinstance(sub, DonchianTrend):
            sub.state = {s: {"peak": p} for s, p in self.peaks.items()}
        raw = sub.targets(ctx)
        if isinstance(sub, DonchianTrend):
            self.peaks.update({s: v["peak"] for s, v in sub.state.items()})

        raw = {s: min(w, self.max_weight) for s, w in raw.items()}
        total = sum(raw.values())
        cap = reg["上限"]
        if total > cap and total > 0:
            raw = {s: w * cap / total for s, w in raw.items()}

        # ストップ直後の買い直しを禁じる期間（cooldown=0 なら無効）
        if self.cooldown:
            raw = {s: w for s, w in raw.items()
                   if ctx.i >= self.cool_until.get(s, -1)}

        if self.use_stop:
            raw = self._apply_stops(ctx, raw)

        # 手仕舞い済みの銘柄のピークは捨てる（残すと再エントリー直後に即ストップ）
        held = set(ctx.weights) | set(raw)
        self.peaks = {s: v for s, v in self.peaks.items() if s in held}
        return raw

    def _apply_stops(self, ctx: Context, raw: dict) -> dict:
        """終値ベースのATRトレーリングストップ。"""
        closes, highs, lows = ctx.hist("close"), ctx.hist("high"), ctx.hist("low")
        weights = ctx.weights
        out = dict(raw)
        for sym in list(weights):
            if weights.get(sym, 0.0) <= 0.001:
                continue
            c = closes[sym].dropna()
            if c.empty:
                continue
            price = float(c.iloc[-1])
            self.peaks[sym] = max(self.peaks.get(sym, price), price)
            a = atr(highs[sym].dropna(), lows[sym].dropna(), c, ATR_N)
            if a != a or a <= 0:
                continue
            if price <= self.peaks[sym] - self.atr_mult * a:
                out[sym] = 0.0
                self.peaks.pop(sym, None)
                if self.cooldown:
                    self.cool_until[sym] = ctx.i + self.cooldown
        return out
