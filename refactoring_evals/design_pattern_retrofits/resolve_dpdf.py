"""Resolve DPD-F instances against the latest version of each project.

DPD-F records only (project, class_name, pattern). For every record whose
project is marked ``likely_buildable`` we look for the class at the current
default-branch HEAD of a local clone in ``EVAL_PROJECTS`` and record where it
lives today. Output: ``raw_data/data/dpdf_resolved.json``.
"""

import csv
import json
import re
import subprocess
from pathlib import Path


HERE = Path(__file__).parent
DATA = HERE / "raw_data" / "data"
DP_DIR = Path(
    "/Users/abhiram/Documents/TBE/RefactoringAgentProject/Agent4Refactoring/data/design_patterns"
)
EVAL_PROJECTS = Path("/Users/abhiram/Documents/TBE/evaluation_projects")
OUT = DATA / "dpdf_resolved.json"

TEST_PATH = re.compile(r"(^|/)(src/)?tests?(/|$)|/test/|Test[^/]*\.java$")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def default_ref(repo: Path) -> str:
    """Prefer the remote default branch so local task branches are ignored."""
    for remote in ("upstream", "origin"):
        ref = git(repo, "symbolic-ref", "-q", f"refs/remotes/{remote}/HEAD")
        if ref:
            return ref
    return "HEAD"


def find_class(repo: Path, ref: str, class_name: str) -> tuple[list[str], str]:
    files = git(repo, "ls-tree", "-r", "--name-only", ref).splitlines()
    by_name = [f for f in files if f.endswith(f"/{class_name}.java")]
    if by_name:
        return by_name, "file_name"
    decl = rf"\b(class|interface|enum|record)\s+{class_name}\b"
    hits = git(repo, "grep", "-l", "-E", decl, ref, "--", "*.java").splitlines()
    return [h.split(":", 1)[1] for h in hits], "nested_declaration"


def pick_primary(paths: list[str]) -> str | None:
    main = [p for p in paths if not TEST_PATH.search(p)]
    pool = main or paths
    return pool[0] if len(pool) == 1 else None


def _supertypes(cls: str, src: str) -> list[str]:
    m = re.search(rf"\b(class|interface)\s+{cls}\b[^{{]*", src)
    if not m:
        return []
    header = re.sub(r"<[^<>]*>", "", m.group(0))
    sup = re.findall(r"\b(?:extends|implements)\s+([\w.,\s]+)", header)
    return [
        s.strip().split(".")[-1] for part in sup for s in part.split(",") if s.strip()
    ]


def _wraps_supertype(cls: str, src: str) -> bool:
    return any(
        re.search(rf"\b{t}(<[^>]*>)?\s+\w+\s*[;=),]", src)
        for t in _supertypes(cls, src)
    )


def structural_check(pattern: str, cls: str, src: str) -> str:
    """Cheap evidence that the pattern is still present. pass/fail/not_checked."""
    checks = {
        "Singleton": lambda: bool(
            re.search(rf"\bprivate\s+{cls}\s*\(", src)
            and re.search(rf"\bstatic\b[^;(]*\b{cls}\b", src)
        ),
        "Builder": lambda: bool(
            re.search(r"\bbuild\w*\s*\(", src) and re.search(r"return\s+this\s*;", src)
        ),
        "Visitor": lambda: bool(re.search(r"\bvisit\w*\s*\(", src)),
        "Observer": lambda: bool(
            re.search(
                r"\b(update|notify\w*|on[A-Z]\w*|\w+Changed|handle\w*Event)\s*\(", src
            )
        ),
        "FactoryMethod": lambda: bool(
            re.search(r"\b(create|new|make)[A-Z]\w*\s*\(", src)
        ),
        "AbstractFactory": lambda: len(
            set(re.findall(r"\b(?:create|new|make)([A-Z]\w*)\s*\(", src))
        )
        >= 2,
        "Prototype": lambda: bool(
            re.search(r"\b(clone|copy|deepCopy)\s*\(\s*\)", src)
            and not re.search(r"\bextends\s+\w*Wrapper\b", src)
        ),
        "Proxy": lambda: _wraps_supertype(cls, src),
        "Decorator": lambda: _wraps_supertype(cls, src),
        "Adapter": lambda: bool(_supertypes(cls, src)),
        "Memento": lambda: bool(
            re.search(r"\b(get|set|restore)State\w*\s*\(", src)
            or re.search(rf"\b{cls}\b[^{{]*\bimplements\s+[^{{]*Memento\b", src)
        ),
        "Facade": lambda: len(
            set(
                re.findall(
                    r"\n\s*(?:private|protected)\s+(?:final\s+)?([A-Z]\w*)(?:<[^>]*>)?\s+\w+\s*[;=]",
                    src,
                )
            )
            - {"String", "Object", "List", "Map", "Set", "Collection", "Logger", "Log"}
        )
        >= 3,
    }
    check = checks.get(pattern)
    if check is None:
        return "not_checked"
    return "pass" if check() else "fail"


def main() -> None:
    dpdf = json.loads((DATA / "dpdf_dataset.json").read_text())
    meta = {
        r["Project Name"]: r
        for r in csv.DictReader(open(DP_DIR / "project_metadata.csv"))
    }
    refs: dict[str, tuple[str, str, str]] = {}
    out = []
    for rec in dpdf:
        project = rec["project_name"]
        m = meta[project]
        if not rec["pattern"] or m["likely_buildable"] != "True":
            continue
        repo = EVAL_PROJECTS / project
        row = {
            "dpdf_id": rec["id"],
            "project": project,
            "class_name": rec["class_name"],
            "pattern": rec["pattern"],
            "repo_url": m["URL"].replace("git://", "https://").removesuffix(".git"),
            "stars": int(m["Stars"] or 0),
            "archived": m["Archived"] == "True",
            "last_push": m["Last Push"],
        }
        if not (repo / ".git").exists():
            out.append({**row, "status": "repo_not_cloned"})
            continue
        if project not in refs:
            ref = default_ref(repo)
            refs[project] = (
                ref,
                git(repo, "rev-parse", ref),
                git(repo, "log", "-1", "--format=%cI", ref),
            )
        ref, sha, date = refs[project]
        paths, how = find_class(repo, ref, rec["class_name"])
        primary = pick_primary(paths)
        source = git(repo, "show", f"{sha}:{primary}") if primary else ""
        out.append(
            {
                **row,
                "status": "found" if paths else "not_found",
                "commit": sha,
                "commit_date": date,
                "match": how if paths else None,
                "candidate_paths": paths,
                "file_path": primary,
                "file_lines": source.count("\n") if source else None,
                "structural_check": (
                    structural_check(rec["pattern"], rec["class_name"], source)
                    if source
                    else "not_checked"
                ),
            }
        )
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    counts: dict[str, int] = {}
    for r in out:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"wrote {len(out)} records to {OUT}: {counts}")


if __name__ == "__main__":
    main()
