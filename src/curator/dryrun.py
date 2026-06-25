from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .plan import Plan


@dataclass
class TreeNode:
    children: dict[str, "TreeNode"] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)


def render_destination_tree(plan: Plan) -> str:
    base = destination_tree_base(plan)
    root = TreeNode()

    for operation in plan.operations:
        if not operation.dest:
            continue
        dest = Path(operation.dest)
        try:
            relative = dest.relative_to(base)
        except ValueError:
            relative = Path(dest.name or str(dest))
        add_path(root, relative)

    lines = render_node(root, depth=0)
    if not lines:
        lines = ["(no planned files)"]
    return "\n".join(lines) + "\n"


def write_dryrun_file(plan: Plan, source: Path) -> Path:
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"dry-mode source must be a directory: {source}")
    path = source / "DRYRUN.txt"
    path.write_text(render_destination_tree(plan), encoding="utf-8")
    return path


def destination_tree_base(plan: Plan) -> Path:
    if plan.metadata.get("kind") == "organize" and plan.metadata.get("library"):
        return Path(str(plan.metadata["library"])) / "Originals"
    destinations = [Path(operation.dest) for operation in plan.operations if operation.dest]
    if not destinations:
        return Path(".")
    return Path(os.path.commonpath([str(path) for path in destinations]))


def add_path(root: TreeNode, relative: Path) -> None:
    parts = relative.parts
    if not parts:
        return
    node = root
    for part in parts[:-1]:
        node = node.children.setdefault(part, TreeNode())
    node.files.append(parts[-1])


def render_node(node: TreeNode, *, depth: int) -> list[str]:
    indent = "    " * depth
    lines: list[str] = []
    for name in sorted(node.children):
        lines.append(f"{indent}{name}/")
        lines.extend(render_node(node.children[name], depth=depth + 1))
    for name in sorted(node.files):
        lines.append(f"{indent}{name}")
    return lines
