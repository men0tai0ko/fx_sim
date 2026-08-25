"""
執行モデル — 現金・ポジション・コスト
------------------------------------------------------------
現物のみ・レバレッジなし（両建て・スワップ計算も無し）。目標ウェイト方式で、
「総資産の何%をその通貨ペアに持つか」を渡すとリバランスする。

crypto_sim の broker.py と機構は同一。違うのはコスト前提だけ。

コスト前提（FX）:
  国内の主要FX会社はスプレッドのみで、別建ての売買手数料が無いところが多い。
  そのためFEE=0とし、スプレッド相当のコストをSLIPPAGEとして表現する。
    SLIPPAGE 0.01%（USD/JPYで概算0.3銭前後、GBP/JPY等の広めのペアも
    ある程度カバーできる保守的な値。実際のスプレッドは通貨ペア・時間帯・
    ブローカーで変わるため、あくまで概算）

未対応（今後の課題。README「既知の限界」参照）:
  - レバレッジ・証拠金・ロスカット
  - スワップポイント（金利差調整）。特にAUD/JPY・GBP/JPYは無視できない額になりうる
"""

from dataclasses import dataclass, field

FEE = 0.0
SLIPPAGE = 0.0001
MIN_TRADE_JPY = 3_000       # これ未満の細かい売買はしない
REBALANCE_BAND = 0.02       # 目標との差がこの幅（総資産比）を超えたときだけ動かす


@dataclass
class Position:
    qty: float = 0.0
    cost_jpy: float = 0.0   # 取得原価の合計（コスト込み）


@dataclass
class Trade:
    date: object
    symbol: str
    side: str               # "買" / "売"
    qty: float
    price: float            # 約定単価（スリッページ込み）
    amount_jpy: float       # 受渡金額（コスト込み）
    realized_jpy: float     # 売却時の実現損益。買いは 0
    reason: str = ""


@dataclass
class Broker:
    cash: float
    fee: float = FEE
    slippage: float = SLIPPAGE
    positions: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)

    # ---- 評価 ----
    def position_value(self, prices: dict) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            p = prices.get(sym)
            if p is None or p != p:      # NaN は価格不明 → 原価で暫定評価
                total += pos.cost_jpy
            else:
                total += pos.qty * p
        return total

    def equity(self, prices: dict) -> float:
        return self.cash + self.position_value(prices)

    def weights(self, prices: dict) -> dict:
        eq = self.equity(prices)
        if eq <= 0:
            return {}
        out = {}
        for sym, pos in self.positions.items():
            p = prices.get(sym)
            if p is not None and p == p:
                out[sym] = pos.qty * p / eq
        return out

    # ---- 執行 ----
    def _sell(self, date, sym: str, qty: float, price: float, reason: str) -> None:
        pos = self.positions.get(sym)
        if pos is None or qty <= 0:
            return
        qty = min(qty, pos.qty)
        fill = price * (1 - self.slippage)
        gross = qty * fill
        proceeds = gross * (1 - self.fee)
        share = qty / pos.qty if pos.qty > 0 else 1.0
        cost_part = pos.cost_jpy * share
        realized = proceeds - cost_part

        self.cash += proceeds
        pos.qty -= qty
        pos.cost_jpy -= cost_part
        if pos.qty <= 1e-12:
            del self.positions[sym]
        self.trades.append(Trade(date, sym, "売", qty, fill, proceeds, realized, reason))

    def _buy(self, date, sym: str, budget: float, price: float, reason: str) -> None:
        budget = min(budget, self.cash)
        if budget < MIN_TRADE_JPY:
            return
        fill = price * (1 + self.slippage)
        qty = budget * (1 - self.fee) / fill
        self.cash -= budget
        pos = self.positions.setdefault(sym, Position())
        pos.qty += qty
        pos.cost_jpy += budget
        self.trades.append(Trade(date, sym, "買", qty, fill, budget, 0.0, reason))

    def rebalance(self, date, prices: dict, targets: dict, reason: str = "") -> None:
        """
        targets: {銘柄: 目標ウェイト(0〜1)}。合計が1を超える場合は正規化して現物内に収める。
        売り→買いの順に執行して、買い付け余力を先に作る。
        """
        tradable = {s: p for s, p in prices.items() if p is not None and p == p and p > 0}
        eq = self.equity(prices)
        if eq <= 0:
            return

        targets = {s: max(0.0, w) for s, w in targets.items() if s in tradable}
        total_w = sum(targets.values())
        if total_w > 1.0:
            targets = {s: w / total_w for s, w in targets.items()}

        current = self.weights(prices)

        # 1) 売り（目標ウェイトを下回らせる分）
        for sym in list(self.positions):
            if sym not in tradable:
                continue
            tgt = targets.get(sym, 0.0)
            cur = current.get(sym, 0.0)
            if cur - tgt <= REBALANCE_BAND and tgt > 0:
                continue
            if cur <= tgt:
                continue
            sell_jpy = (cur - tgt) * eq
            if sell_jpy < MIN_TRADE_JPY:
                continue
            qty = sell_jpy / tradable[sym]
            self._sell(date, sym, qty, tradable[sym], reason or "リバランス")

        # 売却で手数料・スリッページぶん現金が減っている。eq を更新しないまま
        # 買い予算を計算すると、実際より総資産をわずかに大きく見積もることになる
        # （_buy側で現金残高にはキャップされるため資金超過にはならないが、
        #  目標ウェイトへの追随精度がコスト分だけ甘くなる）。
        eq = self.equity(prices)

        # 2) 買い
        for sym, tgt in targets.items():
            cur = self.weights(prices).get(sym, 0.0)
            if tgt - cur <= REBALANCE_BAND:
                continue
            budget = (tgt - cur) * eq
            self._buy(date, sym, budget, tradable[sym], reason or "リバランス")

    def liquidate(self, date, prices: dict, reason: str) -> None:
        for sym in list(self.positions):
            p = prices.get(sym)
            if p is not None and p == p and p > 0:
                self._sell(date, sym, self.positions[sym].qty, p, reason)

    # ---- 保存・復元（リアルタイム運用でプロセスをまたぐため。今は未使用） ----
    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "positions": {s: {"qty": p.qty, "cost_jpy": p.cost_jpy}
                          for s, p in self.positions.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Broker":
        b = cls(cash=float(d["cash"]))
        b.positions = {s: Position(**v) for s, v in d.get("positions", {}).items()}
        return b
