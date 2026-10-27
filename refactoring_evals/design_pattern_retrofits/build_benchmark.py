"""Build benchmark.json and benchmark_lite.json from all prior mining efforts.

Every task is one design-pattern instance with two project states that are
equivalent in external behavior:

* ``pattern``    -- the code contains the pattern instance.
* ``no_pattern`` -- the code does not (pre-retrofit history, or a synthesized
  reversal on a branch/patch).

``subtasks.apply`` starts from ``no_pattern`` and should reach ``pattern``;
``subtasks.remove`` starts from ``pattern`` and should reach ``no_pattern``.
A subtask is ``available`` only when its starting state exists.

Run ``python resolve_dpdf.py`` first; then ``python build_benchmark.py``.
"""

import json
import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path


HERE = Path(__file__).parent
DATA = HERE / "raw_data" / "data"
EVAL_PROJECTS = Path("/Users/abhiram/Documents/TBE/evaluation_projects")
A4R = Path("/Users/abhiram/Documents/TBE/RefactoringAgentProject/Agent4Refactoring")
DP_DIR = A4R / "data" / "design_patterns"
VERIFIED_CASES_DOC = (
    A4R
    / "tesis/backups_y_referencias/RelatedWork-20260611T151228Z-3-001"
    / "RelatedWork/apply-pattern-casos-verificados.md"
)

SOURCE_FILES = {
    "tesis_verified_cases": str(VERIFIED_CASES_DOC),
    "tesis_pipeline": str(DATA / "tesis_master_dataset.json"),
    "validated_benchmarks": str(DATA / "validated_benchmarks.json"),
    "application_tasks": str(DATA / "application_tasks.json"),
    "pattern_candidates": str(DATA / "pattern_candidates.json"),
    "removal_tasks": str(DATA / "removal_tasks.jsonl"),
    "reversal_branches": str(DATA / "reversal_branches.json"),
    "local_reversal_branch": str(EVAL_PROJECTS),
    "dpdf": str(DATA / "dpdf_dataset.json"),
    "dpdf_resolved": str(DATA / "dpdf_resolved.json"),
}

REPO_URLS = {
    "AxonFramework": "AxonFramework/AxonFramework",
    "OpenRefine": "OpenRefine/OpenRefine",
    "activemq": "apache/activemq",
    "ant": "apache/ant",
    "camel": "apache/camel",
    "camunda": "camunda/camunda",
    "cayenne": "apache/cayenne",
    "checkstyle": "checkstyle/checkstyle",
    "commons-collections": "apache/commons-collections",
    "cucumber-jvm": "cucumber/cucumber-jvm",
    "cxf": "apache/cxf",
    "dropwizard": "dropwizard/dropwizard",
    "elasticsearch": "elastic/elasticsearch",
    "flink": "apache/flink",
    "gson": "google/gson",
    "guava": "google/guava",
    "hadoop": "apache/hadoop",
    "hbase": "apache/hbase",
    "hibernate-orm": "hibernate/hibernate-orm",
    "jackrabbit": "apache/jackrabbit",
    "jackson-databind": "FasterXML/jackson-databind",
    "jbehave-core": "jbehave/jbehave-core",
    "jhotdraw": "wumpz/jhotdraw",
    "jsoup": "jhy/jsoup",
    "junit4": "junit-team/junit4",
    "kafka": "apache/kafka",
    "maven": "apache/maven",
    "metrics": "dropwizard/metrics",
    "netty": "netty/netty",
    "retrofit": "square/retrofit",
    "spring-boot": "spring-projects/spring-boot",
    "vert.x": "eclipse-vertx/vert.x",
}

# Remove-subtask target role / apply-subtask target role per pattern.
ROLE_TARGETS = {
    "Builder": ("Builder", "Product"),
    "Strategy": ("Strategy", "Context"),
    "Adapter": ("Adapter", "Target"),
    "Decorator": ("Decorator", "Component"),
    "Composite": ("Composite", "Component"),
    "State": ("State", "Context"),
    "Visitor": ("Visitor", "Element"),
}

VERIFICATION_RANK = {
    "manual_case_study": 0,
    "human_validated": 1,
    "manual_reversal": 1,
    "manual_case_study_borderline": 2,
    "llm_validated": 3,
    "tool_detected": 4,
}
ORIGIN_RANK = {"upstream_history": 0, "synthesized_branch": 1, "synthesized_patch": 2}
VERIFICATION_LEVELS = {
    "manual_case_study": "Hand-verified retrofit commit (tesis case study).",
    "manual_case_study_borderline": "Hand-reviewed, but the pattern fit is weak.",
    "human_validated": "Mined candidate confirmed by a human reviewer.",
    "manual_reversal": "Upstream pattern instance with a hand-made reversal branch.",
    "llm_validated": "Mined retrofit commit approved by an LLM only.",
    "tool_detected": "DPD-F detection, re-located at the latest repository HEAD.",
}
LITE_SELECTION = (
    "Up to 2 tasks per pattern from distinct projects, ranked by: both states "
    "available, verification level, upstream history over synthesized reversals, "
    "then fewer red flags (target pre-existed, feature-sized commit, LLM saw no "
    "code), extra assets (alternatives, prompts), and smaller diffs. Excluded: "
    "borderline cases, ambiguous/test/archived targets, and DPD-F hits that fail "
    "the structural check or are under 30 lines. Remove-only tasks are used only "
    "for patterns with no eligible complete task."
)
NON_RETROFIT_SUBJECT = re.compile(
    r"\b(revert|merge|rename[sd]?|naming|addendum|typo)\b", re.I
)


# ---------------------------------------------------------------- git helpers


def git(project: str, *args: str) -> str | None:
    res = subprocess.run(
        ["git", "-C", str(EVAL_PROJECTS / project), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.stdout.strip() if res.returncode == 0 else None


def file_exists(project: str, commit: str, path: str) -> bool:
    return git(project, "cat-file", "-e", f"{commit}:{path}") is not None


def commit_info(project: str, sha: str) -> dict | None:
    out = git(project, "show", "-s", "--format=%H%n%P%n%cI%n%s", sha)
    if not out:
        return None
    full, parents, when, subject = (out.split("\n") + [""] * 4)[:4]
    status = git(project, "show", "--format=", "--name-status", "-M", full) or ""
    added, modified, deleted = [], [], []
    for line in status.splitlines():
        kind, *paths = line.split("\t")
        path = paths[-1]
        if not path.endswith(".java"):
            continue
        bucket = {"A": added, "D": deleted}.get(kind[0], modified)
        bucket.append(path)
    return {
        "commit": full,
        "parent": parents.split()[0] if parents else None,
        "date": when,
        "subject": subject,
        "java_files_added": added,
        "java_files_modified": modified,
        "java_files_deleted": deleted,
    }


def find_paths(project: str, commit: str, class_name: str) -> list[str]:
    """Files at ``commit`` declaring ``class_name`` (``Outer.Inner`` supported)."""
    outer, _, inner = class_name.partition(".")
    files = git(project, "ls-tree", "-r", "--name-only", commit) or ""
    paths = [f for f in files.splitlines() if f.endswith(f"/{outer}.java")]
    if not inner or not paths:
        return paths
    member = inner if re.fullmatch(r"[A-Z]\w*", inner) else None
    decl = (
        rf"(class|interface|enum|record)[[:space:]]+{member}([^A-Za-z0-9_]|$)"
        if member
        else rf"[^A-Za-z0-9_]{inner}[[:space:]]*\("
    )
    hits = git(project, "grep", "-l", "-E", decl, commit, "--", *paths) or ""
    return [h.split(":", 1)[1] for h in hits.splitlines()]


def locate(project: str, commit: str, class_name: str, hints: list[str]) -> list[str]:
    """Prefer recorded paths when valid at ``commit``; otherwise search by name."""
    valid = [p for p in hints if file_exists(project, commit, p)]
    if valid and "." not in class_name:
        return valid
    found = find_paths(project, commit, class_name)
    return [p for p in found if p in valid] or found


def path_at(project: str, commit: str, current_path: str) -> str | None:
    """Follow renames of ``current_path`` back to its name at ``commit``."""
    log = (
        git(
            project,
            "log",
            "--follow",
            "--name-only",
            "--format=@@%H",
            _default_ref(project),
            "--",
            current_path,
        )
        or ""
    )
    for block in log.split("@@")[1:]:
        sha, *names = [x for x in block.splitlines() if x]
        if sha == commit and names:
            return names[0]
    return None


# ---------------------------------------------------------------- task model


@dataclass
class Target:
    class_name: str | None
    file_paths: list[str]

    def as_dict(self) -> dict:
        return {"class_name": self.class_name, "file_paths": self.file_paths}


def repo_url(project: str) -> str:
    return f"https://github.com/{REPO_URLS[project]}"


def commit_state(project: str, sha: str, origin: str, **extra) -> dict:
    return {
        "kind": "commit",
        "origin": origin,
        "repo_url": repo_url(project),
        "commit": sha,
        **extra,
    }


def make_task(
    *,
    pattern: str,
    project: str,
    repo: str,
    remove_target: Target,
    apply_target: Target | None,
    pattern_state: dict | None,
    no_pattern_state: dict | None,
    verification: str,
    provenance: list[dict],
    id_suffix: str,
    context: dict | None = None,
    roles: dict | None = None,
) -> dict:
    apply_target = apply_target or remove_target
    has_p, has_np = pattern_state is not None, no_pattern_state is not None
    apply_instruction = (
        f"Apply the {pattern} pattern by introducing {remove_target.class_name}."
        if apply_target is remove_target
        else f"Apply the {pattern} pattern to {apply_target.class_name}."
    )
    return {
        "task_id": f"{pattern.lower()}_{project.lower()}_{id_suffix}",
        "pattern": pattern,
        "project": project,
        "repo_url": repo,
        "language": "Java",
        "states": {"pattern": pattern_state, "no_pattern": no_pattern_state},
        "subtasks": {
            "apply": {
                "available": has_np,
                "start_state": "no_pattern",
                "reference_state": "pattern" if has_p else None,
                "target": apply_target.as_dict(),
                "instruction": apply_instruction,
                "prompts": {},
            },
            "remove": {
                "available": has_p,
                "start_state": "pattern",
                "reference_state": "no_pattern" if has_np else None,
                "target": remove_target.as_dict(),
                "instruction": (
                    f"Remove the {pattern} pattern implemented by "
                    f"{remove_target.class_name}."
                ),
                "prompts": {},
            },
        },
        "completeness": (
            "complete" if has_p and has_np else "remove_only" if has_p else "apply_only"
        ),
        "verification": verification,
        "roles": roles or {},
        "context": context or {},
        "alternatives": [],
        "checks": {},
        "provenance": provenance,
    }


def prov(source: str, record_id, **extra) -> dict:
    return {
        "source": source,
        "file": SOURCE_FILES[source],
        "record_id": record_id,
        **extra,
    }


def history_task(
    *,
    pattern: str,
    project: str,
    sha: str,
    remove_cls: str | None,
    apply_cls: str | None,
    remove_paths: list[str] | None,
    apply_paths: list[str] | None,
    verification: str,
    provenance: list[dict],
    context: dict,
    roles: dict | None = None,
) -> dict | None:
    """Task whose states are an upstream commit (pattern) and its parent."""
    info = commit_info(project, sha)
    if info is None:
        print(f"  ! commit {sha} not found in {project}; skipped")
        return None
    full, parent = info["commit"], info["parent"]
    remove_paths = (
        locate(project, full, remove_cls, remove_paths or []) if remove_cls else []
    )
    preexisting = find_paths(project, parent, remove_cls) if remove_cls else []
    apply_paths = (
        locate(project, parent, apply_cls, apply_paths or []) if apply_cls else []
    )
    task = make_task(
        pattern=pattern,
        project=project,
        repo=repo_url(project),
        remove_target=Target(remove_cls, remove_paths),
        apply_target=Target(apply_cls, apply_paths) if apply_cls else None,
        pattern_state=commit_state(project, full, "upstream_history"),
        no_pattern_state=commit_state(project, parent, "upstream_history"),
        verification=verification,
        provenance=provenance,
        id_suffix=full[:7],
        context={
            **context,
            "commit_subject": info["subject"],
            "commit_date": info["date"],
        },
        roles=roles,
    )
    task["checks"] = {
        "java_files_added": len(info["java_files_added"]),
        "java_files_modified": len(info["java_files_modified"]),
        "java_files_deleted": len(info["java_files_deleted"]),
        "remove_target_in_pattern_state": bool(remove_paths),
        "remove_target_preexisted_in_no_pattern_state": bool(preexisting),
        "apply_target_in_no_pattern_state": bool(apply_paths),
        "modifies_existing_code": bool(info["java_files_modified"]),
        "likely_feature_addition": len(info["java_files_added"]) >= 5
        and len(info["java_files_added"]) > len(info["java_files_modified"]),
        "subject_suggests_non_retrofit": bool(
            NON_RETROFIT_SUBJECT.search(info["subject"])
        ),
    }
    return task


# ---------------------------------------------------------------- sources

# (case, project, sha, pattern, apply target, remove target, bad smell, borderline)
VERIFIED_CASES = [
    (
        1,
        "hbase",
        "0d46d56e18000f6f41afa1daf5775117c50ace11",
        "Builder",
        "HColumnDescriptor",
        "HColumnDescriptor",
        "Long Parameter List + Telescoping Constructor",
        False,
    ),
    (
        2,
        "hbase",
        "b0d78134964fca0ff03ac434771fa0e4c669fb06",
        "Builder",
        "StoreFile",
        "StoreFile.WriterBuilder",
        "Factory method explosion",
        False,
    ),
    (
        3,
        "hbase",
        "0142c09217aa4f93cbf7410bbc8c2bce20ff772f",
        "Builder",
        "KeyValue",
        "CellBuilder",
        "Long Parameter List (7-arg KeyValue constructor)",
        False,
    ),
    (
        4,
        "hbase",
        "547d46cfac34a088b7fb6984453c98fcd6f56977",
        "Builder",
        "RawAsyncTable",
        "RawAsyncTable.CoprocessorServiceBuilder",
        "Long Parameter List with nullable parameters",
        False,
    ),
    (
        5,
        "hbase",
        "07e0a30efa332ab451e5f5729dd8257eced82c4d",
        "Builder",
        "HTable",
        "TableBuilder",
        "Mutable state / thread-unsafe setters",
        False,
    ),
    (
        6,
        "hbase",
        "a0cbfb8ea6a7e33f21a1add8bc4c288ea04a6ec2",
        "Builder",
        "HBaseTestingUtility",
        "StartMiniClusterOption",
        "Telescoping method overloads",
        False,
    ),
    (
        7,
        "junit4",
        "d96a05f8d33ed781c701c58dd285bf6464112719",
        "Builder",
        "FailOnTimeout",
        "FailOnTimeout.Builder",
        "Telescoping constructor",
        False,
    ),
    (
        8,
        "junit4",
        "755a82f05ea6cfb884d72041b404fc1c8129f92c",
        "Builder",
        "Timeout",
        "Timeout.Builder",
        "Copy constructor with boolean flag",
        False,
    ),
    (
        9,
        "junit4",
        "f8bceceac2931aee1a315eb7bbac717810959812",
        "Builder",
        "Description",
        "DescriptionBuilder",
        "Constructor with 4+ parameters",
        False,
    ),
    (
        10,
        "junit4",
        "759061a31775afd29ff99fd2a55969bd6aaa8c2f",
        "Strategy",
        "RuleFieldValidator",
        "RuleFieldValidator.RuleValidator",
        "Type code + conditional dispatch",
        False,
    ),
    (
        11,
        "junit4",
        "cb6abf6fc729f07c5d60b0756e91af444400b4b5",
        "FactoryMethod",
        "Parameterized",
        "ParametersRunnerFactory",
        "Hard-coded runner instantiation in protected method",
        False,
    ),
    (
        12,
        "activemq",
        "76381791e8c18abb98c2d203ba5b902dd73a8a38",
        "Strategy",
        "FactoryFinder",
        "FactoryFinder.ObjectFactory",
        "Hard-coded class loading",
        False,
    ),
    (
        13,
        "activemq",
        "24404740a16d6b7d8afd28845cf30c35abf7406a",
        "Strategy",
        "TcpTransport",
        "TcpTransportFactory",
        "Hard-coded socket instantiation",
        False,
    ),
    (
        14,
        "ant",
        "b0bde5550598d2f86534bc0e599c0326caba90bd",
        "Adapter",
        "ProjectHelper",
        "TaskAdapter",
        "Incompatible interface",
        False,
    ),
    (
        15,
        "ant",
        "3885afdd77f0f1619377c813800780c711983ed8",
        "Strategy",
        "FTP",
        "FTP.FTPSystemType",
        "Primitive obsession + switch on string",
        False,
    ),
    (
        16,
        "ant",
        "536a84c211f50992fedce887f3120414eec39068",
        "Strategy",
        "FTP",
        "FTP.LanguageCode",
        "Primitive obsession + switch on string",
        False,
    ),
    (
        17,
        "guava",
        "ef386251e5cc485fb72890fff67830340635e44b",
        "Builder",
        "Interners",
        "Interners.InternerBuilder",
        "Missing configuration point (fixed factory methods)",
        False,
    ),
    (
        18,
        "jbehave-core",
        "e1965c6b1f0e24db57299eba5b010c8a8257a39e",
        "Strategy",
        "CrossReference",
        "PendingStepStrategy",
        "Boolean flag selects algorithm",
        True,
    ),
    (
        19,
        "ant",
        "94f6a318179719d562532927019cf20936c57e03",
        "FactoryMethod",
        "EnumeratedAttribute",
        "EnumeratedAttribute.getInstance",
        "Repeated instantiation code at call sites",
        True,
    ),
    (
        20,
        "ant",
        "1e8307708de490453db28c89f777eb4a9c08767b",
        "FactoryMethod",
        "XSLTProcess",
        "TraXLiaison",
        "Hard-coded XSLT processor",
        True,
    ),
]


def from_verified_cases() -> list[dict]:
    tasks = []
    for (
        case,
        project,
        sha,
        pattern,
        apply_cls,
        remove_cls,
        smell,
        borderline,
    ) in VERIFIED_CASES:
        t = history_task(
            pattern=pattern,
            project=project,
            sha=sha,
            remove_cls=remove_cls,
            apply_cls=apply_cls,
            remove_paths=None,
            apply_paths=None,
            verification="manual_case_study_borderline"
            if borderline
            else "manual_case_study",
            provenance=[prov("tesis_verified_cases", f"Caso {case}")],
            context={"bad_smell": smell},
        )
        if t:
            tasks.append(t)
    return tasks


def _role_target(roles: dict, role: str) -> Target:
    files = [f for f in roles.get(role, []) if f["path"].endswith(".java")]
    names = [f["name"] for f in roles.get(role, [])]
    if not names:
        return Target(None, [])
    first = files[0] if files else roles[role][0]
    return Target(first["name"].removesuffix(".java"), [first["path"]] if files else [])


def _llm_had_code(smell: str) -> bool:
    s = smell.lower()
    return not any(
        k in s
        for k in (
            "no has adjuntado",
            "not included the source",
            "no has incluido",
            "no se ha",
            "no has proporcionado",
            "not provided",
        )
    )


# Tesis birth commits checked by hand and found wrong (record source -> real birth).
BIRTH_OVERRIDES = {
    # 9992cae is a revert merge; HDFS-1974 introduced the HAState hierarchy.
    "State_9992cae5.xml": "73f2092b7351b9cc13d0e12bc4ade6c470934ee8",
}


def from_tesis_pipeline() -> list[dict]:
    raw = json.loads((DATA / "tesis_master_dataset.json").read_text())
    tasks = []
    for inst in raw["refactoring_dataset"]["instances"]:
        decision = inst["llm_analysis"]["decision"]
        if not decision.upper().startswith("APPROVED"):
            continue
        pattern, roles = inst["pattern_name"], inst["roles"]
        remove_role, apply_role = ROLE_TARGETS[pattern]
        rt, at = _role_target(roles, remove_role), _role_target(roles, apply_role)
        if rt.class_name is None:
            continue
        if at.class_name is None and pattern == "Builder":
            at = Target(rt.class_name.removesuffix("Builder") or None, [])
        sha: str = BIRTH_OVERRIDES.get(inst["source"]) or inst["birth_commit"]
        context = {"llm_decision": decision.split("\n")[0][:300]}
        if sha != inst["birth_commit"]:
            context["birth_commit_corrected_from"] = inst["birth_commit"]
        t = history_task(
            pattern=pattern,
            project=inst["project"],
            sha=sha,
            remove_cls=rt.class_name,
            apply_cls=at.class_name,
            remove_paths=rt.file_paths,
            apply_paths=None,
            verification="llm_validated",
            provenance=[prov("tesis_pipeline", inst["source"])],
            context=context,
            roles={
                r: [f["path"] if f["path"].endswith(".java") else f["name"] for f in fs]
                for r, fs in roles.items()
            },
        )
        if t:
            t["checks"]["llm_analysis_saw_code"] = _llm_had_code(
                inst["llm_analysis"]["smell"]
            )
            tasks.append(t)
    return tasks


def from_candidates() -> list[dict]:
    tasks = []
    for c in json.loads((DATA / "pattern_candidates.json").read_text()):
        if c.get("human_validation") is False or not c["pattern_file"].endswith(
            ".java"
        ):
            continue
        project = Path(c["repo_path"]).name
        cls, path = c["class_name"], c["original_file_path"] or c["pattern_file"]
        if not file_exists(project, c["birth_commit_sha"], path):
            old = path_at(project, c["birth_commit_sha"], c["pattern_file"])
            if old and old.endswith(".java"):
                cls, path = Path(old).stem, old
        t = history_task(
            pattern=c["pattern"],
            project=project,
            sha=c["birth_commit_sha"],
            remove_cls=cls,
            apply_cls=None,
            remove_paths=[path],
            apply_paths=None,
            verification="human_validated"
            if c.get("human_validation")
            else "llm_validated",
            provenance=[prov("pattern_candidates", c["id"])],
            context={
                "detection_source": c["detection_source"],
                "detection_reasoning": c.get("detection_reasoning"),
                "current_class_name": c["class_name"],
                "current_file_path": c["pattern_file"],
            },
        )
        if t:
            tasks.append(t)
    return tasks


def _branch_state(project: str, branch: str, base_ref: str) -> tuple[dict, dict] | None:
    head = git(project, "rev-parse", branch)
    base = git(project, "merge-base", branch, base_ref)
    if not head or not base:
        return None
    pushed = (git(project, "branch", "-r", "--contains", head) or "").split()
    files = (git(project, "diff", "--name-only", base, head) or "").splitlines()
    pattern_state = commit_state(project, base, "upstream_history")
    no_pattern_state = {
        "kind": "branch",
        "origin": "synthesized_branch",
        "repo_url": repo_url(project),
        "base_commit": base,
        "branch": branch,
        "head_commit": head,
        "local_repo_path": str(EVAL_PROJECTS / project),
        "pushed_to_remote_refs": pushed,
        "files_changed": len(files),
        "non_java_files_changed": [f for f in files if not f.endswith(".java")],
    }
    return pattern_state, no_pattern_state


# Branches found in local clones but missing from reversal_branches.json.
LOCAL_REVERSALS = [
    (
        "cayenne",
        "reversal/PropertyVisitor",
        "Visitor",
        "PropertyVisitor",
        "Invert PropertyVisitor using instanceof checks",
    ),
    (
        "camunda",
        "reversal/ModelElementVisitor",
        "Visitor",
        "ModelElementVisitor",
        "Invert ModelElementVisitor hierarchy into Consumer",
    ),
    (
        "jackrabbit",
        "reversal/query-node-visitor",
        "Visitor",
        "QueryNodeVisitor",
        "Remove Default and Traversing QueryNodeVisitor implementations",
    ),
    (
        "cxf",
        "openhands_remove_WSDLServiceBuilder",
        "Builder",
        "WSDLServiceBuilder",
        "OpenHands removal of WSDLServiceBuilder",
    ),
    (
        "checkstyle",
        "remove-AuditListener-manual",
        "Observer",
        "AuditListener",
        "Manual removal of the AuditListener observer interface",
    ),
]
REVERSAL_CLASS = {
    "flink-datatype-visitor": "DataTypeVisitor",
    "flink-stream-compression-decorator": "StreamCompressionDecorator",
    "flink-datatype-precision-fixer": "DataTypePrecisionFixer",
    "flink-datatype-precision-fixer-instanceof": "DataTypePrecisionFixer",
    "jsoup-nodevisitor": "NodeVisitor",
    "jsoup-nodevisitor-sonnet": "NodeVisitor",
    "openrefine-recordvisitor": "RecordVisitor",
}


def _default_ref(project: str) -> str:
    for remote in ("upstream", "origin"):
        ref = git(project, "symbolic-ref", "-q", f"refs/remotes/{remote}/HEAD")
        if ref:
            return ref
    return "HEAD"


def from_reversal_branches() -> list[dict]:
    specs = [
        (
            r["project"],
            r["branch"],
            r["pattern"],
            REVERSAL_CLASS[r["id"]],
            r["description"],
            prov("reversal_branches", r["id"]),
        )
        for r in json.loads((DATA / "reversal_branches.json").read_text())
    ] + [
        (p, b, pat, cls, desc, prov("local_reversal_branch", f"{p}:{b}"))
        for p, b, pat, cls, desc in LOCAL_REVERSALS
    ]
    tasks = []
    for project, branch, pattern, cls, desc, pv in specs:
        states = _branch_state(project, branch, _default_ref(project))
        if states is None:
            print(f"  ! branch {project}:{branch} not found; skipped")
            continue
        p_state, np_state = states
        paths = find_paths(project, p_state["commit"], cls)
        tasks.append(
            make_task(
                pattern=pattern,
                project=project,
                repo=repo_url(project),
                remove_target=Target(cls, paths),
                apply_target=None,
                pattern_state=p_state,
                no_pattern_state=np_state,
                verification="manual_reversal",
                provenance=[pv],
                id_suffix=re.sub(r"[^a-z0-9]+", "", cls.lower()),
                context={"reversal_description": desc},
            )
        )
    return tasks


def removal_patch_alternatives() -> dict[str, list[dict]]:
    """Synthesized no-pattern patches, keyed by pattern_candidates id."""
    latest: dict[str, dict] = {}
    for line in (DATA / "removal_tasks.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["status"] == "done" and r["patch_path"]:
            latest[r["task_id"]] = r
    by_content: dict[str, str] = {}
    out: dict[str, list[dict]] = {}
    for task_id, r in sorted(latest.items()):
        patch = Path(r["patch_path"])
        text = patch.read_text()
        if text in by_content:
            print(f"  ! {task_id} patch duplicates {by_content[text]}; skipped")
            continue
        by_content[text] = task_id
        header = re.search(r"^From ([0-9a-f]{40})", text, re.M)
        project = Path(r["repo_path"]).name
        patch_commit = header.group(1) if header else r["git_commit_sha"]
        base = git(project, "rev-parse", f"{patch_commit}^") if patch_commit else None
        out.setdefault(r["candidate_id"], []).append(
            {
                "variant": r["variant_name"],
                "variant_id": r["variant_id"],
                "pattern": commit_state(project, base, "upstream_history")
                if base
                else None,
                "no_pattern": {
                    "kind": "patch",
                    "origin": "synthesized_patch",
                    "repo_url": repo_url(project),
                    "base_commit": base,
                    "patch_path": str(patch),
                    "patch_commit": patch_commit,
                    "local_branch": task_id
                    if git(project, "rev-parse", "--verify", "-q", task_id)
                    else None,
                    "files_changed": [
                        m.split(" b/")[-1]
                        for m in re.findall(r"^diff --git (.+)$", text, re.M)
                    ],
                },
                "prompt": r["task_prompt"],
                "provenance": prov("removal_tasks", task_id),
            }
        )
    return out


def from_dpdf() -> list[dict]:
    tasks, seen = [], set()
    for r in json.loads((DATA / "dpdf_resolved.json").read_text()):
        if r["status"] != "found":
            continue
        key = (r["project"], r["class_name"], r["pattern"])
        if key in seen:
            continue
        seen.add(key)
        paths = [r["file_path"]] if r["file_path"] else r["candidate_paths"]
        t = make_task(
            pattern=r["pattern"],
            project=r["project"],
            repo=r["repo_url"],
            remove_target=Target(r["class_name"], paths),
            apply_target=None,
            pattern_state={
                "kind": "commit",
                "origin": "latest_head",
                "repo_url": r["repo_url"],
                "commit": r["commit"],
                "commit_date": r["commit_date"],
            },
            no_pattern_state=None,
            verification="tool_detected",
            provenance=[
                prov("dpdf", r["dpdf_id"]),
                prov("dpdf_resolved", r["dpdf_id"]),
            ],
            id_suffix=re.sub(r"[^a-z0-9]+", "", r["class_name"].lower()),
        )
        t["subtasks"]["apply"]["needs_synthesis"] = True
        t["checks"] = {
            "structural_check": r["structural_check"],
            "target_file_lines": r["file_lines"],
            "target_path_ambiguous": r["file_path"] is None,
            "target_in_test_code": all("/test" in p.lower() for p in paths),
            "repo_archived": r["archived"],
            "repo_stars": r["stars"],
            "repo_last_push": r["last_push"],
        }
        tasks.append(t)
    return tasks


# ---------------------------------------------------------------- merging


def merge(tasks: list[dict]) -> list[dict]:
    """Merge records describing the same instance.

    Two records match when they share (project, pattern, class), or when records
    from *different* sources point at the same upstream retrofit commit (e.g. a
    tesis case naming ``TableBuilder`` and a candidate naming
    ``TableBuilderBase``). Distinct classes mined from one source stay
    separate tasks even when they share a commit. The best-verified complete
    record wins; the others contribute provenance and, when they carry a
    different no-pattern state, an alternative.
    """

    def keys(t: dict) -> list[tuple]:
        project, pattern = t["project"].lower(), t["pattern"]
        cls = t["subtasks"]["remove"]["target"]["class_name"]
        out = [("class", project, pattern, cls)]
        np_state = t["states"]["no_pattern"]
        if np_state and np_state["origin"] == "upstream_history":
            out.append(("commit", project, pattern, t["states"]["pattern"]["commit"]))
        return out

    groups: list[list[dict]] = []
    index: dict[tuple, tuple[int, str]] = {}
    for t in tasks:
        source = t["provenance"][0]["source"]
        hit = None
        for k in keys(t):
            if k in index and (k[0] == "class" or index[k][1] != source):
                hit = index[k][0]
                break
        if hit is None:
            hit = len(groups)
            groups.append([])
        groups[hit].append(t)
        for k in keys(t):
            index.setdefault(k, (hit, source))
    merged = []
    for group in groups:
        group.sort(
            key=lambda t: (
                t["completeness"] != "complete",
                VERIFICATION_RANK[t["verification"]],
            )
        )
        keep = group[0]
        for other in group[1:]:
            keep["provenance"] += [
                p for p in other["provenance"] if p not in keep["provenance"]
            ]
            np_state = other["states"]["no_pattern"]
            if np_state and np_state != keep["states"]["no_pattern"]:
                keep["alternatives"].append(
                    {"pattern": other["states"]["pattern"], "no_pattern": np_state}
                )
        merged.append(keep)
    seen: dict[str, int] = {}
    for t in merged:
        n = seen.get(t["task_id"], 0)
        seen[t["task_id"]] = n + 1
        if n:
            cls = re.sub(
                r"[^a-z0-9]+",
                "",
                (t["subtasks"]["remove"]["target"]["class_name"] or "").lower(),
            )
            t["task_id"] = f"{t['task_id']}_{cls}"
    return merged


def attach_extras(tasks: list[dict]) -> None:
    by_prov = {(p["source"], p["record_id"]): t for t in tasks for p in t["provenance"]}
    for cand_id, alts in removal_patch_alternatives().items():
        t = by_prov.get(("pattern_candidates", cand_id))
        if t is None:
            print(f"  ! removal patches for {cand_id} have no task; skipped")
            continue
        for alt in alts:
            t["provenance"].append(alt.pop("provenance"))
            t["alternatives"].append(alt)
    app = {
        a["task_id"]: a["prompts"]
        for a in json.loads((DATA / "application_tasks.json").read_text())
    }
    for cand_id, prompts in app.items():
        t = by_prov.get(("pattern_candidates", cand_id))
        if t:
            t["subtasks"]["apply"]["prompts"] = prompts
            t["provenance"].append(prov("application_tasks", cand_id))
    for vb in json.loads((DATA / "validated_benchmarks.json").read_text()):
        for t in tasks:
            p = t["states"]["pattern"]
            if p and p.get("commit") == vb["v1_hash"]:
                if vb.get("task-terse"):
                    t["subtasks"]["apply"]["prompts"].setdefault(
                        "terse", {"prompt": vb["task-terse"]}
                    )
                t["context"]["justification"] = vb["justification"]
                t["provenance"].append(
                    prov("validated_benchmarks", vb["id"] or vb["class_name"])
                )


# ---------------------------------------------------------------- lite


def lite_score(t: dict) -> tuple:
    np_state = t["states"]["no_pattern"]
    checks = t["checks"]
    return (
        t["completeness"] != "complete",
        VERIFICATION_RANK[t["verification"]],
        ORIGIN_RANK.get(np_state["origin"], 3) if np_state else 3,
        not checks.get("modifies_existing_code", True),
        bool(checks.get("remove_target_preexisted_in_no_pattern_state")),
        not checks.get("llm_analysis_saw_code", True),
        bool(checks.get("target_path_ambiguous")),
        bool(checks.get("target_in_test_code")),
        -len(t["alternatives"]) - len(t["subtasks"]["apply"]["prompts"]),
        -checks.get("repo_stars", 0),
        checks.get("java_files_added", 0) + checks.get("java_files_modified", 0),
        len(t["subtasks"]["remove"]["target"]["file_paths"]) == 0,
    )


def eligible_for_lite(t: dict) -> bool:
    c = t["checks"]
    if t["verification"] == "manual_case_study_borderline":
        return False
    if (
        t["subtasks"]["remove"]["available"]
        and not t["subtasks"]["remove"]["target"]["file_paths"]
    ):
        return False
    if t["verification"] == "tool_detected":
        if c["structural_check"] != "pass" or (c["target_file_lines"] or 0) < 30:
            return False
    np_state = t["states"]["no_pattern"]
    if np_state and np_state["origin"] == "upstream_history":
        if not c.get("remove_target_in_pattern_state") or not c.get(
            "modifies_existing_code"
        ):
            return False
        manual = t["verification"] == "manual_case_study"
        if not manual and (
            c.get("remove_target_preexisted_in_no_pattern_state")
            or c.get("likely_feature_addition")
            or c.get("subject_suggests_non_retrofit")
        ):
            return False
    return not (
        c.get("target_path_ambiguous")
        or c.get("target_in_test_code")
        or c.get("repo_archived")
    )


def select_lite(tasks: list[dict], per_pattern: int = 2) -> list[dict]:
    chosen = []
    for pattern in sorted({t["pattern"] for t in tasks}):
        pool = sorted(
            (t for t in tasks if t["pattern"] == pattern and eligible_for_lite(t)),
            key=lite_score,
        )
        picked, projects = [], set()
        for t in pool:
            if len(picked) == per_pattern:
                break
            if t["project"] in projects:
                continue
            if (
                picked
                and t["completeness"] != "complete"
                and picked[0]["completeness"] == "complete"
            ):
                break
            picked.append(t)
            projects.add(t["project"])
        chosen += [
            {
                "task_id": t["task_id"],
                "pattern": t["pattern"],
                "completeness": t["completeness"],
                "verification": t["verification"],
            }
            for t in picked
        ]
    return chosen


# ---------------------------------------------------------------- main


CONFIDENCE_RULES = {
    "dpdf_structural_check_not_passed": (
        "DPD-F hits whose class at the latest HEAD fails (or has no) structural "
        "check for its pattern."
    ),
    "llm_analysis_saw_no_code": (
        "Tesis-pipeline approvals where the LLM reported it was not shown the code."
    ),
    "remove_target_missing_at_pattern_commit": (
        "History tasks whose remove-target class cannot be found at the pattern "
        "commit (wrong birth commit or unresolved rename)."
    ),
}


def confidence_reason(t: dict) -> str | None:
    c = t["checks"]
    if t["verification"] == "tool_detected" and c.get("structural_check") != "pass":
        return "dpdf_structural_check_not_passed"
    if c.get("llm_analysis_saw_code") is False:
        return "llm_analysis_saw_no_code"
    if c.get("remove_target_in_pattern_state") is False:
        return "remove_target_missing_at_pattern_commit"
    return None


def apply_confidence_filter(tasks: list[dict]) -> tuple[list[dict], dict[str, int]]:
    reasons = [confidence_reason(t) for t in tasks]
    kept = [t for t, r in zip(tasks, reasons) if r is None]
    return kept, dict(Counter(r for r in reasons if r))


def _md_table(header: list[str], rows: Iterable[Sequence]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return lines + ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]


def write_summary_md(tasks: list[dict], lite: list[dict]) -> None:
    by_id = {t["task_id"]: t for t in tasks}
    lite_count = Counter(x["pattern"] for x in lite)
    patterns = sorted({t["pattern"] for t in tasks})
    pattern_rows = []
    for p in patterns:
        ts = [t for t in tasks if t["pattern"] == p]
        ver = Counter(t["verification"] for t in ts)
        pattern_rows.append(
            [
                p,
                len(ts),
                sum(t["completeness"] == "complete" for t in ts),
                sum(t["completeness"] == "remove_only" for t in ts),
                len({t["project"] for t in ts}),
                ", ".join(f"{k} {v}" for k, v in sorted(ver.items())),
                lite_count[p],
            ]
        )
    lite_rows = []
    for x in lite:
        t = by_id[x["task_id"]]
        np_state = t["states"]["no_pattern"]
        lite_rows.append(
            [
                f"`{t['task_id']}`",
                t["project"],
                f"`{t['subtasks']['remove']['target']['class_name']}`",
                np_state["kind"] if np_state else "none (remove only)",
                t["verification"],
            ]
        )
    source_count = Counter(p["source"] for t in tasks for p in t["provenance"])
    lines = [
        "# Benchmark Summary",
        "",
        "Generated by `build_benchmark.py`. Do not edit by hand.",
        "",
        f"- `benchmark.json`: **{len(tasks)} tasks**, "
        f"{len(patterns)} patterns, {len({t['project'] for t in tasks})} projects",
        f"- `benchmark_lite.json`: **{len(lite)} tasks**",
        "",
        "## Completeness and verification",
        "",
        *_md_table(
            ["completeness", "tasks"],
            sorted(Counter(t["completeness"] for t in tasks).items()),
        ),
        "",
        *_md_table(
            ["verification", "tasks", "meaning"],
            [
                [k, v, VERIFICATION_LEVELS[k]]
                for k, v in sorted(Counter(t["verification"] for t in tasks).items())
            ],
        ),
        "",
        "## By pattern",
        "",
        *_md_table(
            [
                "pattern",
                "tasks",
                "complete",
                "remove only",
                "projects",
                "verification",
                "lite",
            ],
            pattern_rows,
        ),
        "",
        "## Lite set",
        "",
        *_md_table(
            ["task", "project", "remove target", "no-pattern state", "verification"],
            lite_rows,
        ),
        "",
        "## Records contributing to tasks (after merging)",
        "",
        *_md_table(["source", "records"], sorted(source_count.items())),
        "",
    ]
    (HERE / "BENCHMARK_SUMMARY.md").write_text("\n".join(lines))


def main() -> None:
    raw: list[dict] = []
    for name, fn in [
        ("tesis_verified_cases", from_verified_cases),
        ("reversal_branches", from_reversal_branches),
        ("pattern_candidates", from_candidates),
        ("tesis_pipeline", from_tesis_pipeline),
        ("dpdf", from_dpdf),
    ]:
        got = fn()
        print(f"{name}: {len(got)} tasks")
        raw += got
    tasks = merge(raw)
    attach_extras(tasks)
    tasks, excluded = apply_confidence_filter(tasks)
    print(f"confidence filter: dropped {sum(excluded.values())} tasks {excluded}")
    tasks.sort(
        key=lambda t: (t["pattern"], VERIFICATION_RANK[t["verification"]], t["task_id"])
    )
    assert len({t["task_id"] for t in tasks}) == len(tasks)

    meta = {
        "name": "design_pattern_retrofits",
        "generated": date.today().isoformat(),
        "generator": "build_benchmark.py",
        "description": (__doc__ or "").strip(),
        "sources": SOURCE_FILES,
        "verification_levels": VERIFICATION_LEVELS,
        "confidence_filter": {"rules": CONFIDENCE_RULES, "excluded_counts": excluded},
        "task_count": len(tasks),
    }
    (HERE / "benchmark.json").write_text(
        json.dumps({**meta, "tasks": tasks}, indent=1) + "\n"
    )
    lite = select_lite(tasks)
    (HERE / "benchmark_lite.json").write_text(
        json.dumps(
            {
                "name": "design_pattern_retrofits_lite",
                "generated": meta["generated"],
                "benchmark": "benchmark.json",
                "selection": LITE_SELECTION,
                "task_count": len(lite),
                "tasks": lite,
            },
            indent=1,
        )
        + "\n"
    )
    summarize(tasks, lite)


def summarize(tasks: list[dict], lite: list[dict]) -> None:
    write_summary_md(tasks, lite)
    print(f"\nbenchmark.json: {len(tasks)} tasks")
    print(" completeness:", dict(Counter(t["completeness"] for t in tasks)))
    print(" verification:", dict(Counter(t["verification"] for t in tasks)))
    rows = Counter((t["pattern"], t["completeness"]) for t in tasks)
    for p in sorted({t["pattern"] for t in tasks}):
        complete, remove_only = rows[(p, "complete")], rows[(p, "remove_only")]
        print(f"  {p:16} complete={complete:3} remove_only={remove_only:3}")
    print(f"benchmark_lite.json: {len(lite)} tasks")
    for t in lite:
        print(f"  {t['task_id']:55} {t['completeness']:12} {t['verification']}")


if __name__ == "__main__":
    main()
