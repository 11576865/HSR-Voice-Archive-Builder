from __future__ import annotations

from collections.abc import Iterable

# Small project-owned core terminology set. These are terminology constraints,
# not a copy of game dialogue. Keep this list conservative: only stable,
# established names/terms should be hard-enforced.
CORE_GLOSSARY: dict[str, str] = {
    "Evanescia": "绯英",
    "Planarcadia": "二相乐园",
    "Phantasmoon Games": "幻月游戏",
    "Wishpower": "愿力",
    "Supplicant": "谒者",
    "Graphia": "绘世",
    "Yao Guang": "爻光",
    "Fulwish": "满愿",
    "Stellar Jade": "星琼",
}


def relevant_glossary(
    glossary: dict[str, str],
    texts: Iterable[str],
) -> dict[str, str]:
    haystack = "\n".join(str(x) for x in texts).casefold()
    return {
        source: target
        for source, target in glossary.items()
        if source.casefold() in haystack
    }
