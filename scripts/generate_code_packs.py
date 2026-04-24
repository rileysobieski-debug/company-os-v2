"""Generate flattened per-branch code packs for direct paste into
Gemini / Grok / any LLM reviewer. Output is written to
`<vault>/v6-code-packs/<branch-slug>.md`. Each file contains:

    - header with branch name and commit
    - change summary (added / modified / renamed / deleted)
    - full text of every new or modified file with language fencing

Usage: `python scripts/generate_code_packs.py`
"""
from __future__ import annotations

import pathlib
import subprocess

BRANCHES = [
    ("week2-safepath", "feature/week2-safepath"),
    ("week2-day2-hardening", "feature/week2-day2-hardening"),
    ("week2-walls-layer", "feature/week2-walls-layer"),
    ("weeks-4-5-memory-layer", "feature/weeks-4-5-memory-layer"),
    ("weeks-6-7-mcp-adapter", "feature/weeks-6-7-mcp-adapter"),
    ("weeks-6-7-settlement", "feature/weeks-6-7-settlement"),
]

LANG = {
    "py": "python", "sql": "sql", "yml": "yaml", "yaml": "yaml",
    "md": "markdown", "lock": "text", "txt": "text", "json": "json",
    "html": "html", "css": "css", "js": "javascript", "ts": "typescript",
}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True,
        check=True, encoding="utf-8", errors="replace",
    ).stdout


def resolve_head(branch: str) -> str:
    return git("rev-parse", "--short", f"origin/{branch}").strip()


def classify(diff_output: str):
    added: list[str] = []
    modified: list[str] = []
    renamed: list[tuple[str, str]] = []
    deleted: list[str] = []
    for line in diff_output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("A"):
            added.append(parts[1])
        elif status.startswith("M"):
            modified.append(parts[1])
        elif status.startswith("R"):
            renamed.append((parts[1], parts[2]))
        elif status.startswith("D"):
            deleted.append(parts[1])
    return added, modified, renamed, deleted


def build_pack(slug: str, branch: str, out_dir: pathlib.Path) -> int:
    head = resolve_head(branch)
    diff = git("diff", "--name-status", "origin/main", f"origin/{branch}")
    added, modified, renamed, deleted = classify(diff)

    lines: list[str] = [
        f"# Company OS v6 -- Code Pack: {branch}",
        "",
        f"**Commit:** {head}",
        "**Base:** origin/main",
        "",
    ]
    lines.append(f"**Files added ({len(added)}):**")
    lines += [f"- {p}" for p in added]
    lines.append("")
    if modified:
        lines.append(f"**Files modified ({len(modified)}):**")
        lines += [f"- {p}" for p in modified]
        lines.append("")
    if renamed:
        lines.append(f"**Files renamed ({len(renamed)}):**")
        lines += [f"- {a} -> {b}" for a, b in renamed]
        lines.append("")
    if deleted:
        lines.append(f"**Files deleted ({len(deleted)}):**")
        lines += [f"- {p}" for p in deleted]
        lines.append("")

    lines += [
        "---", "",
        "## Full text of every new or modified file",
        "",
    ]

    files = added + modified + [b for _, b in renamed]
    for path in files:
        try:
            content = git("show", f"origin/{branch}:{path}")
        except subprocess.CalledProcessError:
            continue
        ext = pathlib.Path(path).suffix.lstrip(".")
        lang = LANG.get(ext, "text")
        lines.append(f"### `{path}`")
        lines.append("")
        lines.append(f"```{lang}")
        lines.append(content.rstrip())
        lines.append("```")
        lines.append("")

    out = out_dir / f"{slug}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out.stat().st_size


def main() -> None:
    out_dir = pathlib.Path(
        "C:/Users/riley_edejtwi/Obsidian Vault/v6-code-packs",
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Writing packs to {out_dir}")
    for slug, branch in BRANCHES:
        size = build_pack(slug, branch, out_dir)
        print(f"  {slug}.md  ({size // 1024} KB)")


if __name__ == "__main__":
    main()
