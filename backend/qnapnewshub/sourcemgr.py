"""取得先の管理（/news/sources のページ用）: 設定にある全ソースの一覧（形式・分野・間隔・有効/無効・実績）と、一括の操作。

2026-11-24 利用者「設定の中にボタンを置き、別の管理ページに飛ばして、疎通確認だったりとか、これまでの取得実績、最終取得日時などを…サイトごとに確認できたりとか、新しいサイトの追加、削除などができるように」。
設定（sources.json）が持つのは: rss/pages/github/codeberg/discourse/reddit/hackernews/huggingface と intervals（ソースごとの間隔）・disabled（止めているソース）。
ここは設定の読み書きだけ（取得の実績は fetchlog）。権限の判定は router。
"""
from __future__ import annotations

from urllib.parse import urlsplit

from qnapnewshub import presets, sources

CONFIG_KINDS = ("rss", "page", "github", "codeberg", "discourse", "reddit", "hackernews", "huggingface")
FORMAT_LABELS = {"rss": "RSS/Atom", "page": "一覧の巡回", "github": "GitHub", "codeberg": "Codeberg", "discourse": "フォーラム", "reddit": "Reddit",
                 "hackernews": "Hacker News", "huggingface": "Hugging Face", "search": "検索", "steam": "Steam", "arxiv": "arXiv"}


def key_of(kind: str, value: str = "") -> str:
    return {"rss": f"rss:{value}", "page": f"page:{value}", "hackernews": "hn", "huggingface": "hf"}.get(kind, f"{kind}:{value}")


def format_of(kind: str, value: str) -> str:
    """表示用の形式（rss は URL から Steam・arXiv・検索・GitHub の Atom を見分ける）。"""
    if kind == "rss":
        host = (urlsplit(value).hostname or "").lower()
        if host == "store.steampowered.com":
            return "steam"
        if host == "rss.arxiv.org":
            return "arxiv"
        if host == "news.google.com":
            return "search"
        if host == "github.com":
            return "github"
    return kind


_OFFICIAL = ("openai.com", "anthropic.com", "nvidia.com", "amd.com", "intel.com", "microsoft.com", "meta.com", "ai.meta.com", "deepmind.google", "blog.google",
             "github.blog", "huggingface.co", "pytorch.org", "kernel.org", "mozilla.org", "python.org", "rust-lang.org", "llvm.org", "gnome.org", "kde.org",
             "store.steampowered.com", "steampowered.com", "aws.amazon.com", "cloud.google.com", "developer.nvidia.com", "rocm.blogs.amd.com", "qnap.com",
             "apple.com", "mistral.ai", "deepseek.com", "x.ai", "ollama.com", "ggml.ai", "go.dev", "nodejs.org", "debian.org", "ubuntu.com", "canonical.com")
_PRESS = ("gigazine.net", "phoronix.com", "itmedia.co.jp", "impress.co.jp", "nikkei.com", "nhk.or.jp", "reuters.com", "bbc.com", "bbc.co.uk", "techcrunch.com",
          "theverge.com", "arstechnica.com", "wired.com", "engadget.com", "4gamer.net", "gamespot.com", "cnn.com", "bloomberg.com", "ascii.jp", "gizmodo.com",
          "zdnet.com", "cnet.com", "thehackernews.com", "bleepingcomputer.com", "servethehome.com", "tomshardware.com", "anandtech.com", "videocardz.com",
          "news.google.com", "yomiuri.co.jp", "asahi.com", "mainichi.jp", "sankei.com", "jiji.com", "kyodonews.jp", "forbes.com", "wsj.com", "nytimes.com",
          "theguardian.com", "venturebeat.com", "infoq.com", "publickey1.jp", "watch.impress.co.jp", "gamer.ne.jp", "famitsu.com", "dengekionline.com")
_BLOG = ("medium.com", "note.com", "qiita.com", "zenn.dev", "hatenablog.com", "hatenablog.jp", "hatena.ne.jp", "wordpress.com", "blogspot.com", "substack.com",
         "dev.to", "tistory.com", "ameblo.jp", "livedoor.jp", "fc2.com", "hashnode.dev", "ghost.io")
_COMMUNITY = ("news.ycombinator.com", "reddit.com", "lobste.rs", "stackoverflow.com", "stackexchange.com", "discourse.org", "slashdot.org", "5ch.net", "v2ex.com")
_PAPER = ("arxiv.org", "rss.arxiv.org", "openreview.net", "biorxiv.org", "medrxiv.org", "ssrn.com", "acm.org", "ieee.org", "paperswithcode.com", "nature.com",
          "science.org", "sciencedirect.com", "springer.com", "aclanthology.org", "jmlr.org", "neurips.cc", "mlr.press", "semanticscholar.org")


def _under(host: str, names: tuple[str, ...]) -> bool:
    return any(host == n or host.endswith("." + n) for n in names)


def infer_origin(kind: str, value: str, name: str = "") -> str:
    """出どころの種類をドメインの規則で推定する（official・press・blog・community・paper・other）。利用者が決めた値（設定の origins）が先。
    GitHub・Codeberg の Release は公式、Hacker News・Reddit・フォーラム・Hugging Face の話題はコミュニティ、arXiv などは論文、
    既知の報道・ブログのドメイン、`blog.`・`developer.`・`newsroom.` で始まるホストは公式、名前やホストに news を含むものは報道。"""
    if kind in ("github", "codeberg"):
        return "official"
    if kind in ("hackernews", "reddit", "discourse"):
        return "community"
    if kind == "huggingface":
        return "community"
    host = (urlsplit(value).hostname or "").lower() if value.startswith("http") else ""
    if host == "github.com":
        return "official"
    if _under(host, _PAPER):
        return "paper"
    if _under(host, _COMMUNITY):
        return "community"
    if _under(host, _BLOG):
        return "blog"
    if _under(host, _PRESS):
        return "press"
    if _under(host, _OFFICIAL) or host.startswith(("blog.", "developer.", "developers.", "newsroom.", "press.", "engineering.")):
        return "official"
    if "news" in host or "news" in name.lower() or "ニュース" in name:
        return "press"
    return "other"


def entries(cfg: dict) -> list[dict]:
    """設定にあるソース全部（止めているものも）。{key, kind(設定の種類), format, name, value, domain, enabled, interval(分・0=既定), groups}。"""
    off, ivs = set(cfg.get("disabled", [])), cfg.get("intervals", {})
    fixed, origins, cats = set(cfg.get("fixed", [])), cfg.get("origins", {}), cfg.get("categories", {})
    rows: list[tuple[str, str, str]] = []
    rows += [("rss", f["url"], f["name"]) for f in cfg["rss"]]
    rows += [("page", f["url"], f["name"]) for f in cfg.get("pages", [])]
    rows += [("github", r, f"GitHub {r}") for r in cfg["github"]]
    rows += [("codeberg", r, f"Codeberg {r}") for r in cfg["codeberg"]]
    rows += [("discourse", d, d) for d in cfg["discourse"]]
    rows += [("reddit", s, f"r/{s}") for s in cfg["reddit"]]
    if cfg.get("hackernews"):
        rows.append(("hackernews", "", "Hacker News"))
    if cfg.get("huggingface"):
        rows.append(("huggingface", "", "Hugging Face"))
    out = []
    for kind, value, name in rows:
        key = key_of(kind, value)
        domain = (urlsplit(value).hostname or "") if value.startswith("https://") else {"github": "github.com", "codeberg": "codeberg.org", "reddit": "reddit.com",
                                                                                         "discourse": value, "hackernews": "news.ycombinator.com", "huggingface": "huggingface.co"}.get(kind, "")
        out.append({"key": key, "kind": kind, "format": format_of(kind, value), "name": name, "value": value, "domain": domain,
                    "enabled": key not in off, "interval": ivs.get(key, 0), "groups": presets.groups_of(kind, value),
                    "fixed": key in fixed, "origin": origins.get(key) or infer_origin(kind, value, name), "origin_set": key in origins, "category": cats.get(key, "")})
    return out


def _set_off(cfg: dict, keys: set[str], off: bool) -> None:
    cur = [k for k in cfg.get("disabled", []) if k not in keys]
    cfg["disabled"] = cur + (sorted(keys) if off else [])


def remove(cfg: dict, keys: set[str]) -> None:
    cfg["rss"] = [f for f in cfg["rss"] if key_of("rss", f["url"]) not in keys]
    cfg["pages"] = [f for f in cfg.get("pages", []) if key_of("page", f["url"]) not in keys]
    for kind in ("github", "codeberg", "discourse", "reddit"):
        cfg[kind] = [v for v in cfg[kind] if key_of(kind, v) not in keys]
    if "hn" in keys:
        cfg["hackernews"] = False
    if "hf" in keys:
        cfg["huggingface"] = False
    cfg["intervals"] = {k: v for k, v in cfg.get("intervals", {}).items() if k not in keys}
    cfg["disabled"] = [k for k in cfg.get("disabled", []) if k not in keys]
    cfg["fixed"] = [k for k in cfg.get("fixed", []) if k not in keys]
    cfg["origins"] = {k: v for k, v in cfg.get("origins", {}).items() if k not in keys}
    cfg["categories"] = {k: v for k, v in cfg.get("categories", {}).items() if k not in keys}


def add(cfg: dict, items: list[dict]) -> int:
    """items: [{kind, value, name?, interval?}]。すでにあるものは足さない（間隔だけ付ける）。足した数を返す。形の検査は save_config（normalize）。"""
    added = 0
    for it in items:
        kind, value = str(it.get("kind", "")), str(it.get("value", "")).strip()
        if kind not in CONFIG_KINDS:
            raise sources.ConfigError(f"知らない種類です: {kind[:20]}")
        key = key_of(kind, value)
        if key not in {e["key"] for e in entries(cfg)}:
            if kind in ("rss", "page"):
                cfg["rss" if kind == "rss" else "pages"].append({"name": str(it.get("name") or "").strip()[:60], "url": value})
            elif kind == "hackernews":
                cfg["hackernews"] = True
            elif kind == "huggingface":
                cfg["huggingface"] = True
            else:
                cfg[kind].append(value)
            added += 1
            fresh = True
        else:
            fresh = False
        if it.get("interval"):
            cfg.setdefault("intervals", {})[key] = int(it["interval"])
        if fresh:      # 分類・出どころ・無効の指定は、新しく足したものにだけ付ける（すでにある設定を上書きしない）
            if it.get("origin"):
                cfg.setdefault("origins", {})[key] = str(it["origin"])
            if it.get("category"):
                cfg.setdefault("categories", {})[key] = str(it["category"])[:40]
            if it.get("enabled") is False:
                cfg["disabled"] = [*cfg.get("disabled", []), key]
    return added


def apply(cfg: dict, action: str, keys: list[str], interval: int | None = None, origin: str | None = None) -> dict:
    """一括の操作（enable・disable・delete・interval・fixed・auto・origin）を cfg に反映して返す（保存は呼び出し側）。interval=0 は「既定に戻す」。"""
    chosen = set(keys)
    known = {e["key"] for e in entries(cfg)}
    if not chosen or not chosen <= known:
        raise sources.ConfigError("知らないソースが含まれています")
    if action == "delete":
        remove(cfg, chosen)
    elif action in ("enable", "disable"):
        _set_off(cfg, chosen, action == "disable")
    elif action == "interval":
        if interval is None or not (interval == 0 or sources.INTERVAL_MIN <= interval <= sources.INTERVAL_MAX):
            raise sources.ConfigError(f"取得の間隔は {sources.INTERVAL_MIN}〜{sources.INTERVAL_MAX} 分です（0 は既定）")
        ivs = dict(cfg.get("intervals", {}))
        for k in chosen:
            if interval:
                ivs[k] = interval
            else:
                ivs.pop(k, None)
        cfg["intervals"] = ivs
    elif action in ("fixed", "auto"):
        cur = [k for k in cfg.get("fixed", []) if k not in chosen]
        cfg["fixed"] = cur + (sorted(chosen) if action == "fixed" else [])
    elif action == "origin":
        table = dict(cfg.get("origins", {}))
        for k in chosen:
            if origin in (None, "", "auto"):
                table.pop(k, None)      # 推定に戻す
            elif origin in sources.ORIGINS:
                table[k] = origin
            else:
                raise sources.ConfigError("出どころの種類が違います")
        cfg["origins"] = table
    else:
        raise sources.ConfigError("知らない操作です")
    return cfg
