"""只读检查项目文档路由与证据记录；不自动提升结论或重置上下文。"""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from memory_gate import validate_record


def local_links(path):
    text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
    for value in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
        target = value.strip().strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        yield (path.parent / unquote(parsed.path)).resolve()


def check(root):
    root = Path(root).resolve()
    errors, review = [], []
    index = root / "docs/cache/context_index.md"
    owners = set(local_links(index))
    documents = owners | {index, root / "README.md", root / "AGENTS.md"}
    documents |= set((root / "docs").glob("*.md"))
    for doc in sorted(documents):
        if not doc.is_file():
            errors.append(f"缺失文档：{doc}")
            continue
        for target in local_links(doc):
            if not target.exists():
                errors.append(f"失效文件链接：{doc.relative_to(root)} → {target}")
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
