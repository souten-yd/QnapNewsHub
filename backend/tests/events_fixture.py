"""イベントのまとめの試験データ（合成。実データではない）: (出来事の名前, 記事の一覧)。記事 = (取得先, 題, 抜粋, 公開の何時間前, [(物の名前, 種類)])。
同じ出来事の記事（言語をまたぐ）と、同じ会社・同じ分野の別の出来事を混ぜてある。しきい値の確かめ（test_events.py）と docs/DESIGN.md 11 章の実測の元。"""
from __future__ import annotations

GPT = [("OpenAI", "org"), ("GPT-6.2", "model")]
EVENTS: dict[str, list[tuple]] = {
    "gpt62": [
        ("official-a", "OpenAI launches GPT-6.2 with longer context", "OpenAI today released GPT-6.2, a new model with a million token context window.", 5, GPT),
        ("press-b", "GPT-6.2 arrives: what OpenAI's new model changes", "A look at the new GPT-6.2 release from OpenAI and what it means for developers.", 4, GPT),
        ("press-ja", "OpenAI、GPT-6.2を発表 文脈長が大幅に拡大", "OpenAIは新モデルGPT-6.2を公開した。コンテキスト長が拡大される。", 3, GPT),
        ("blog-ja", "【速報】GPT-6.2が登場、開発者への影響は", "OpenAIが発表したGPT-6.2について、開発者向けに要点をまとめた。", 2, GPT),
    ],
    "qwen4": [
        ("official-q", "Alibaba releases Qwen 4 open-weight models", "Alibaba Cloud announced Qwen 4, a family of open-weight language models.", 20, [("Alibaba", "org"), ("Qwen 4", "model")]),
        ("press-q", "Qwen 4 open weights land on Hugging Face", "The Qwen 4 family from Alibaba is now available on Hugging Face.", 18, [("Alibaba", "org"), ("Qwen 4", "model")]),
        ("press-qja", "アリババ、Qwen 4をオープンウェイトで公開", "アリババはQwen 4シリーズを公開した。Hugging Faceで入手できる。", 17, [("アリババ", "org"), ("Qwen 4", "model")]),
    ],
    "llamacpp-qwen4": [
        ("gh-llama", "llama.cpp adds Qwen 4 support in the latest build", "The latest llama.cpp release adds support for running Qwen 4 models locally.", 12, [("llama.cpp", "software"), ("Qwen 4", "model")]),
        ("forum-llama", "llama.cpp now runs Qwen 4 (b9000 release notes)", "Release notes: Qwen 4 architecture supported in llama.cpp.", 11, [("llama.cpp", "software"), ("Qwen 4", "model")]),
    ],
    "openai-lawsuit": [
        ("press-law1", "Authors sue OpenAI over training data", "A group of authors filed a lawsuit against OpenAI over the use of books for training.", 9, [("OpenAI", "org")]),
        ("press-law2", "OpenAI faces new copyright lawsuit from authors", "OpenAI is facing a copyright suit filed by a group of authors this week.", 8, [("OpenAI", "org")]),
        ("press-law3", "作家らがOpenAIを提訴 学習データの著作権めぐり", "作家のグループがOpenAIを著作権侵害で提訴した。", 7, [("OpenAI", "org")]),
    ],
    "nvidia-earnings": [
        ("fin-a", "Nvidia posts record quarterly revenue on data center demand", "Nvidia reported record revenue for the quarter, driven by data center sales.", 30, [("Nvidia", "org")]),
        ("fin-b", "Nvidia earnings beat estimates as data center sales surge", "Nvidia beat analyst estimates with record data center revenue.", 29, [("Nvidia", "org")]),
        ("fin-ja", "エヌビディア、4〜6月期は過去最高の売上 データセンター好調", "エヌビディアの四半期売上は過去最高だった。データセンター向けが好調。", 28, [("エヌビディア", "org")]),
    ],
    "rocm-release": [
        ("gh-rocm", "ROCm 7.2 released with new RDNA4 support", "AMD released ROCm 7.2 which adds support for RDNA4 GPUs.", 15, [("ROCm 7.2", "software"), ("AMD", "org")]),
        ("press-rocm", "AMD ships ROCm 7.2: RDNA4 gets official support", "ROCm 7.2 from AMD brings official RDNA4 support.", 14, [("ROCm 7.2", "software"), ("AMD", "org")]),
    ],
    "rocm-bug": [
        ("forum-rocm", "Anyone else seeing hangs on ROCm with multiple GPUs?", "Running two cards, the system hangs after a few minutes of load.", 13, [("ROCm", "software")]),
    ],
    "amd-driver": [
        ("press-amd", "AMD Radeon driver update fixes game crashes", "A new Radeon Software driver fixes crashes in several games.", 10, [("AMD", "org"), ("Radeon", "product")]),
    ],
    "apple-iphone": [
        ("press-ap1", "Apple unveils iPhone 18 with new camera system", "Apple introduced the iPhone 18 with an upgraded camera.", 40, [("Apple", "org"), ("iPhone 18", "product")]),
        ("press-ap2", "iPhone 18 hands-on: the camera is the big change", "Hands-on with the new iPhone 18 and its camera.", 39, [("Apple", "org"), ("iPhone 18", "product")]),
        ("press-apja", "アップル、iPhone 18を発表 カメラを刷新", "アップルはiPhone 18を発表した。カメラが新しくなった。", 38, [("アップル", "org"), ("iPhone 18", "product")]),
    ],
    "apple-earnings": [
        ("fin-ap", "Apple services revenue hits all-time high in quarterly report", "Apple's services business reached a record in the latest quarter.", 36, [("Apple", "org")]),
    ],
    "kernel": [
        ("lwn", "Linux 7.1 released with new scheduler", "Linus Torvalds released Linux 7.1 featuring a new scheduler.", 25, [("Linux 7.1", "software"), ("Linus Torvalds", "person")]),
        ("press-k", "Linux 7.1 lands: scheduler rework and more", "Linux 7.1 is out with a reworked scheduler.", 24, [("Linux 7.1", "software")]),
    ],
    "tsmc": [
        ("press-t1", "TSMC to build new fab in Arizona", "TSMC announced plans for another fab in Arizona.", 22, [("TSMC", "org"), ("Arizona", "place")]),
    ],
    "misc1": [("rnd1", "Show HN: a tiny SQLite viewer in Rust", "I built a small SQLite viewer in Rust.", 6, [("SQLite", "software"), ("Rust", "tech")])],
    "misc2": [("rnd2", "Weekly thread: what are you working on?", "Share what you're working on this week.", 6, [])],
    "misc3": [("rnd3", "Ask HN: Best way to learn Rust in 2026?", "Looking for resources to learn Rust.", 5, [("Rust", "tech")])],
}
