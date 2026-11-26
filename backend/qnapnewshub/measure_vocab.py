"""C2: 物理量の次元と明示の別名のみで正規化する。外部依存なし。"""
from __future__ import annotations

from decimal import Decimal
import unicodedata
from qnapnewshub.bench import cond_key
from qnapnewshub.measure_quantity import decimal_value

LLM_KEYS = frozenset("model model_params quant quant_variant hw hw_count vram cpu cpu_threads ram mem_bw engine backend engine_version version prompt_tokens gen_tokens ctx concurrency batch ubatch offload speculative flash_attn kv_type power power_limit benchmark".split())
PRICE_KEYS = frozenset("currency region edition channel tax period".split())
DOMAINS = {"llm_perf": LLM_KEYS, "price": PRICE_KEYS}
UNITS = {
    "ns": ("time", "ms", "0.000001"), "µs": ("time", "ms", "0.001"),
    "us": ("time", "ms", "0.001"), "ms": ("time", "ms", "1"),
    "s": ("time", "ms", "1000"), "秒": ("time", "ms", "1000"),
    "min": ("time", "ms", "60000"), "h": ("time", "ms", "3600000"),
    "tok/s": ("token_rate", "tok/s", "1"), "tokens/s": ("token_rate", "tok/s", "1"),
    "tok/min": ("token_rate", "tok/s", "0.01666666666666666666666666667"),
    "B": ("bytes", "B", "1"), "KB": ("bytes", "B", "1000"), "MB": ("bytes", "B", "1000000"),
    "GB": ("bytes", "B", "1000000000"), "TB": ("bytes", "B", "1000000000000"),
    "KiB": ("bytes", "B", "1024"), "MiB": ("bytes", "B", "1048576"), "GiB": ("bytes", "B", "1073741824"),
    "mW": ("power", "W", "0.001"), "W": ("power", "W", "1"), "kW": ("power", "W", "1000"),
    "MHz": ("frequency", "Hz", "1000000"), "GHz": ("frequency", "Hz", "1000000000"),
    "%": ("percent", "%", "1"), "USD": ("currency:USD", "USD", "1"),
    "JPY": ("currency:JPY", "JPY", "1"), "円": ("currency:JPY", "JPY", "1"),
}


def normalize_quantity(value: str, unit: str) -> tuple[Decimal, str, str]:
    number = decimal_value(value)
    dim, canonical, factor = UNITS.get(unit, ("unknown:" + unit, unit, "1"))
    return number * Decimal(factor), canonical, dim


def normalize_condition(key: str, value: str) -> str:
    return cond_key(key, value)
