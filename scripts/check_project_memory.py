"""只读检查项目文档路由与证据记录；不自动提升结论或重置上下文。"""

import argparse
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

from memory_gate import validate_record

HOT_LIMITS = {
    "AGENTS.md": 6144,
    "docs/cache/kernel.md": 3072,
    "docs/cache/context_index.md": 4096,
    "docs/cache/checkpoint.md": 3072,
}


def markdown_text(path):
    return re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)


def link_targets(path):
    text = markdown_text(path)
    for value in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
        target = value.strip().strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            continue
        target = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
        yield target, unquote(parsed.fragment)


def local_links(path):
    for target, _ in link_targets(path):
        yield target


def heading_anchors(path):
    """GitHub-style IDs for the ATX headings used in our current handbooks.

    This is deliberately not a general Markdown renderer. Explicit HTML anchors
    are accepted; inline labels are flattened and duplicate heading IDs counted.
    """
    text = markdown_text(path)
    anchors = set(re.findall(r'(?:id|name)=[\"\']([^\"\']+)[\"\']', text))
    counts = {}
    for title in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M):
        title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title)
        title = re.sub(r"<[^>]*>", "", title).lower()
        slug = "".join(c for c in title if c in "-_ " or unicodedata.category(c)[0] in "LN")
        slug = slug.replace(" ", "-")
        number = counts.get(slug, 0)
        counts[slug] = number + 1
        anchors.add(f"{slug}-{number}" if number else slug)
    return anchors


def hot_budget(root):
    sizes, errors = {}, []
    for name, limit in HOT_LIMITS.items():
        path = root / name
        if not path.is_file():
            errors.append(f"缺失热记忆：{name}")
            continue
        sizes[name] = path.stat().st_size
        if sizes[name] > limit:
            errors.append(f"热记忆超限：{name} {sizes[name]} > {limit} bytes")
    default = sum(size for name, size in sizes.items() if not name.endswith("checkpoint.md"))
    resumed = sum(sizes.values())
    for name, value, limit in (("default", default, 13312), ("resumed", resumed, 16384)):
        if value > limit:
            errors.append(f"热记忆合计超限：{name} {value} > {limit} bytes")
    kernel = root / "docs/cache/kernel.md"
    if kernel.is_file() and len(re.findall(r"^- ", markdown_text(kernel), re.M)) > 8:
        errors.append("kernel主题超过8条")
    return {"files": sizes, "default_bytes": default, "resumed_bytes": resumed}, errors


def check(root):
    root = Path(root).resolve()
    errors, review = [], []
    index = root / "docs/cache/context_index.md"
    owners = set(local_links(index)) if index.is_file() else set()
    documents = owners | {index, root / "README.md", root / "AGENTS.md"}
    documents |= set((root / "docs").glob("*.md"))
    documents |= set((root / "docs/cache").glob("*.md"))
    budget, budget_errors = hot_budget(root)
    errors.extend(budget_errors)
    anchors = {}
    for doc in sorted(documents):
        if not doc.is_file():
            errors.append(f"缺失文档：{doc}")
            continue
        for target, fragment in link_targets(doc):
            if not target.exists():
                errors.append(f"失效文件链接：{doc.relative_to(root)} → {target}")
            elif fragment and target.is_file() and target.suffix == ".md":
                if target not in anchors:
                    anchors[target] = heading_anchors(target)
                if fragment not in anchors[target]:
                    errors.append(f"失效章节链接：{doc.relative_to(root)} → {target.name}#{fragment}")
    seen = set()
    current = 0
    for path in sorted((root / "docs/cache/records").glob("*.json")):
        try:
            record = json.loads(path.read_text())
            ident = record.get("id")
            if ident in seen:
                errors.append(f"重复记录 ID：{ident}")
            seen.add(ident)
            if (root / record.get("owner", "")).resolve() not in owners:
                errors.append(f"记录 owner 未在路由中：{path.name}")
            try:
                validate_record(root, record)
            except (ValueError, TypeError, OSError) as exc:
                if record.get("status") == "verified":
                    errors.append(f"已验证记录失效：{path.name}: {exc}")
                else:
                    review.append(f"仅历史审阅：{path.name}: {exc}")
                continue
            if record.get("status") == "verified" and record.get("recheck") != "always":
                current += 1
            else:
                review.append(f"需现场复查或仅供审阅：{path.name}")
        except (ValueError, TypeError, OSError) as exc:
            errors.append(f"记录读取失败：{path.name}: {exc}")
    return {
        "errors": errors,
        "review_only": review,
        "validated_records": current,
        "documents": len(documents),
        "hot_budget": budget,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = check(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result["errors"]))


if __name__ == "__main__":
    main()
