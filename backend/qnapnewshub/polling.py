"""取得先ごとの取得の状態: ETag・Last-Modified の条件付きの取得、Adaptive Polling、失敗・制限でのバックオフ（ARCHITECTURE 10 章）。

- 状態（etag・last_modified・いまの間隔・次の予定・失敗の連続・新着の間隔の指数平均・304 の当たり）は取得先の source_key ごとにメモリが正。
  ファイル（news/fetch-state.json）へは Write Coordinator のまとめ書きのときに書く（記事のバッファと同じ瞬間に書くので、
  「ETag だけ進んで記事が消えた」は起きない。強制終了では最大 30 分の分が戻り、次の取得でやり直す＝取りこぼしにはならない）。
- 間隔: 基準（取得先ごとの設定か、設定の既定）に対し、最短 = 基準の 1/2・最長 = 基準の 8 倍。
  新着が出た取得の間隔（指数平均）の半分を次の間隔にする（出る頻度の 2 倍で見に行く）。出ないまま長く経てば、その経過の半分まで伸ばす。
  履歴の無いうちは 1.5 倍ずつ伸び縮み。利用者が「固定」を選んだ取得先は基準のまま。
- 失敗・429・503: 基準 × 2^失敗回数（上限 24 時間）。Retry-After があればそれ以上（上限は同じ）。成功で元に戻る。固定でも失敗では延ばす。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from qnapnewshub import writer
from qnapnewshub.config import data_dir

MIN_FACTOR = 0.5
MAX_FACTOR = 8.0
STEP = 1.5
BACKOFF_CAP_MIN = 24 * 60.0
EMA = 0.5
MAX_STATES = 5000

_mem: dict[str, dict[str, dict]] = {}       # 置き場ごとの {source_key: 状態}


def _path() -> Path:
    return data_dir() / "news" / "fetch-state.json"


def _states() -> dict[str, dict]:
    key = str(_path())
    found = _mem.get(key)
    if found is None:
        try:
            raw = json.loads(_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        found = _mem[key] = {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
    return found


def reset_memory() -> None:
    _mem.clear()


def get(key: str) -> dict:
    """その取得先の状態（無ければ空の dict）。"""
    return dict(_states().get(key, {}))


def all_states() -> dict[str, dict]:
    return {k: dict(v) for k, v in _states().items()}


def forget(keys: list[str]) -> None:
    states = _states()
    for k in keys:
        states.pop(k, None)
    _stage(states)


def _stage(states: dict[str, dict]) -> None:
    path = _path()
    if len(states) > MAX_STATES:
        for k in sorted(states, key=lambda k: states[k].get("last_at", 0))[: len(states) - MAX_STATES]:
            states.pop(k)
    snapshot = json.dumps(states, ensure_ascii=False)

    def do() -> None:
        writer.write_file(path, snapshot, name="fetch.state", user=False)

    writer.get().stage_state("fetch-state", do)


# ---- 間隔の計算（純粋な関数。試験が直接呼ぶ） ----

def bounds(base: float) -> tuple[float, float]:
    return base * MIN_FACTOR, base * MAX_FACTOR


def clamp(minutes: float, base: float) -> float:
    lo, hi = bounds(base)
    return max(lo, min(hi, minutes))


def adapt(state: dict, base: float, new: int, now: float) -> None:
    """成功した取得の結果から、次の間隔（分）を state["interval"] に入れる。new は新着の数（304 は 0）。state を直接書き換える。"""
    cur = float(state.get("interval") or base)
    ema = state.get("gap_ema")
    if new > 0:
        last_new = state.get("last_new_at")
        if last_new:
            gap = max((now - last_new) / 60.0, 0.1)
            ema = gap if ema is None else EMA * ema + (1 - EMA) * gap
        state["last_new_at"] = now
        state["gap_ema"] = ema
    if ema is None:
        target = cur / STEP if new > 0 else cur * STEP
    else:
        quiet = (now - (state.get("last_new_at") or now)) / 60.0
        target = max(ema, quiet) / 2.0
    state["interval"] = clamp(target, base)


def backoff_minutes(base: float, fails: int, retry_after: float | None = None) -> float:
    """失敗が fails 回続いたあとの待ち（分）。基準 × 2^fails。Retry-After（秒）があればそれ以上。上限 24 時間。"""
    wait = base * (2 ** max(fails, 1))
    if retry_after:
        wait = max(wait, retry_after / 60.0)
    return min(wait, BACKOFF_CAP_MIN)


def effective_minutes(state: dict, base: float, fixed: bool) -> float:
    """いまの取得の間隔（分）。失敗が続いていればバックオフの値、固定なら基準、そうでなければ自動の値。"""
    if state.get("fails"):
        return float(state.get("backoff") or backoff_minutes(base, int(state["fails"])))
    if fixed or not state.get("interval"):
        return float(base)
    return clamp(float(state["interval"]), base)


# ---- 取得の結果を残す ----

def record(key: str, *, base: float, fixed: bool, ok: bool, new: int = 0, not_modified: bool = False, conditional: bool = False,
           etag: str = "", last_modified: str = "", status: int | None = None, retry_after: float | None = None, now: float | None = None) -> dict:
    """1 回の取得の結果を状態へ。成功: ETag・Last-Modified を覚え、間隔を計算し直す。失敗: 失敗の連続を増やして待ちを延ばす。"""
    now = time.time() if now is None else now
    states = _states()
    st = states.setdefault(key, {})
    st["last_at"] = now
    st["last_status"] = status
    if conditional:
        st["cond"] = int(st.get("cond", 0)) + 1
        if not_modified:
            st["hits"] = int(st.get("hits", 0)) + 1
    if ok:
        st["fails"] = 0
        st.pop("backoff", None)
        st["polls"] = int(st.get("polls", 0)) + 1
        if not not_modified:
            st["etag"], st["last_modified"] = etag[:300], last_modified[:100]
        adapt(st, base, new, now)
        if fixed:
            st["interval"] = float(base)
    else:
        st["fails"] = int(st.get("fails", 0)) + 1
        st["backoff"] = backoff_minutes(base, st["fails"], retry_after)
    st["next_at"] = now + effective_minutes(st, base, fixed) * 60.0
    _stage(states)
    return dict(st)


def conditional_headers(key: str) -> tuple[str, str]:
    st = _states().get(key) or {}
    return str(st.get("etag") or ""), str(st.get("last_modified") or "")


def snapshot(key: str, base: float, fixed: bool) -> dict:
    """画面・診断に出す形: {interval(分), auto(自動か), next_at(epoch か None), fails, hit_rate(0〜100 か None), conditional(回数), hits(304 の回数)}。"""
    st = _states().get(key) or {}
    every = effective_minutes(st, base, fixed)
    last = st.get("last_at")
    cond = int(st.get("cond", 0))
    return {"interval": round(every, 1), "auto": not fixed and not st.get("fails"), "backoff": bool(st.get("fails")), "fails": int(st.get("fails", 0)),
            "next_at": (last + every * 60.0) if last else None, "hit_rate": round(int(st.get("hits", 0)) * 100 / cond) if cond else None,
            "conditional": cond, "hits": int(st.get("hits", 0)), "has_etag": bool(st.get("etag") or st.get("last_modified"))}
