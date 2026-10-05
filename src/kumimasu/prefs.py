from __future__ import annotations

import difflib
from pathlib import Path

import yaml
from pydantic import BaseModel

from .config import PROJECT_FILE, Config, load, user_path
from .model import Rule
from .rules import default_rules, dump_rules, packaged_rules, parse_rules
from .workdir import WorkDir

SCOPES = ("user", "project")
PROJECT_RULES = "kumimasu.rules.yaml"


def _layer_value(cfg: Config, name: str, key: str):
    layer = next((x for x in cfg.layers if x.name == name), None)
    return layer.values.get(key) if layer else None


def rules_target(scope: str, cwd: Path, create: bool) -> tuple[Path, list[str]]:
    if scope not in SCOPES:
        raise ValueError(f"--scope must be one of {', '.join(SCOPES)}")
    cfg = load(None, cwd)
    notes: list[str] = []
    if scope == "user":
        value = _layer_value(cfg, "user", "rules_file") or _layer_value(cfg, "default", "rules_file")
        path = Path(value)
    else:
        value = _layer_value(cfg, "project", "rules_file")
        if value:
            path = Path(value)
        else:
            path = (cwd / PROJECT_RULES).resolve()
            if create:
                cfg_file = cwd / PROJECT_FILE
                text = cfg_file.read_text(encoding="utf-8") if cfg_file.exists() else ""
                sep = "" if not text or text.endswith("\n") else "\n"
                cfg_file.write_text(f"{text}{sep}rules_file: {PROJECT_RULES}\n", encoding="utf-8")
                notes.append(f"{cfg_file} に rules_file: {PROJECT_RULES} を足しました")
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_rules(packaged_rules()), encoding="utf-8")
        notes.append(f"{path} を同梱の既定のルールで作りました")
    return path, notes


def read_target(path: Path) -> list[Rule]:
    return parse_rules(path.read_text(encoding="utf-8"), str(path)) if path.exists() else packaged_rules()


def _index(n: str, size: int) -> int:
    if not n.isdigit() or not 1 <= int(n) <= size:
        raise ValueError(f"number must be 1–{size}")
    return int(n) - 1


def edit_rules(scope: str, action: str, args: list[str], cwd: Path) -> tuple[Path, list[Rule], list[str]]:
    path, notes = rules_target(scope, cwd, create=True)
    rules = read_target(path)
    match action:
        case "on" | "off" if len(args) == 1:
            rules[_index(args[0], len(rules))].on = action == "on"
        case "edit" if len(args) == 2:
            rules[_index(args[0], len(rules))].text = args[1].strip()
        case "add" if len(args) == 1:
            rules.append(Rule(text=args[0].strip()))
        case "rm" if len(args) == 1:
            rules.pop(_index(args[0], len(rules)))
        case _:
            raise ValueError("usage: rules on N | off N | edit N TEXT | add TEXT | rm N --scope user|project")
    path.write_text(dump_rules(rules), encoding="utf-8")
    return path, rules, notes


class Diff(BaseModel):
    n: int
    kind: str
    key: str = ""
    text: str = ""
    old: str = ""
    value: object = None
    default: object = None


def rules_diff(wd: WorkDir) -> list[Diff]:
    cfg = load(wd.root)
    base = default_rules(cfg.rules_path())
    mine = wd.design().rules
    a, b = [r.text for r in base], [r.text for r in mine]
    out: list[Diff] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                if base[i].on != mine[j].on:
                    out.append(Diff(n=0, kind="disabled" if base[i].on else "enabled", text=mine[j].text))
            continue
        olds, news = a[i1:i2], b[j1:j2]
        for k in range(max(len(olds), len(news))):
            if k < len(olds) and k < len(news):
                out.append(Diff(n=0, kind="edited", text=news[k], old=olds[k]))
            elif k < len(news):
                out.append(Diff(n=0, kind="added", text=news[k]))
            else:
                out.append(Diff(n=0, kind="removed", text=olds[k]))
    for n, d in enumerate(out, 1):
        d.n = n
    return out


def _pick(diffs: list[Diff], only: list[str]) -> list[Diff]:
    if not only:
        return diffs
    want = set(only)
    return [d for d in diffs if str(d.n) in want or d.key in want]


def rules_save(wd: WorkDir, scope: str, only: list[str], cwd: Path) -> tuple[Path, list[Diff], list[str]]:
    picked = _pick(rules_diff(wd), only)
    path, notes = rules_target(scope, cwd, create=True)
    rules = read_target(path)
    texts = [r.text for r in rules]
    for d in picked:
        match d.kind:
            case "added":
                if d.text not in texts:
                    rules.append(Rule(text=d.text))
            case "removed":
                rules = [r for r in rules if r.text != d.text]
            case "edited":
                hit = next((r for r in rules if r.text == d.old), None)
                if hit:
                    hit.text = d.text
                elif d.text not in texts:
                    rules.append(Rule(text=d.text))
            case "disabled" | "enabled":
                for r in rules:
                    if r.text == d.text:
                        r.on = d.kind == "enabled"
        texts = [r.text for r in rules]
    path.write_text(dump_rules(rules), encoding="utf-8")
    return path, picked, notes


def prefs_diff(wd: WorkDir) -> list[Diff]:
    cfg = load(wd.root)
    d = wd.design()
    out: list[Diff] = []

    def add(key: str, kind: str, value, default) -> None:
        out.append(Diff(n=len(out) + 1, kind=kind, key=key, value=value, default=default))

    if d.formality != cfg.get("defaults.register"):
        add("defaults.register", "changed", d.formality, cfg.get("defaults.register"))
    if d.drop_list != cfg.get("defaults.drop_list"):
        add("defaults.drop_list", "changed", d.drop_list, cfg.get("defaults.drop_list"))
    always = cfg.get("defaults.avoid") or []
    for topic in d.avoid:
        if topic not in always and topic not in d.avoid_proposed:
            add("defaults.avoid", "added", topic, always)
    for key, n in (("defaults.noise.skip_max", len(d.skip)), ("defaults.noise.aside_max", len(d.aside))):
        if n != cfg.get(key):
            add(key, "changed", n, cfg.get(key))
    if (d.form_prefs or "") != (cfg.get("defaults.forms") or ""):
        add("defaults.forms", "changed", d.form_prefs, cfg.get("defaults.forms"))
    return out


def config_target(scope: str, cwd: Path) -> Path:
    if scope not in SCOPES:
        raise ValueError(f"--scope must be one of {', '.join(SCOPES)}")
    return user_path() if scope == "user" else cwd / PROJECT_FILE


def _set(data: dict, key: str, value) -> None:
    parts = key.split(".")
    cur = data
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def write_config_values(path: Path, values: dict) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    head = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            head.append(line)
        else:
            break
    data = yaml.safe_load(text) or {}
    for k, v in values.items():
        _set(data, k, v)
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(("\n".join(head).rstrip("\n") + "\n" if any(h.strip() for h in head) else "") + body, encoding="utf-8")


def prefs_save(wd: WorkDir, scope: str, only: list[str], cwd: Path) -> tuple[Path, list[Diff]]:
    picked = _pick(prefs_diff(wd), only)
    path = config_target(scope, cwd)
    values: dict = {}
    for d in picked:
        if d.key == "defaults.avoid":
            values.setdefault("defaults.avoid", list(d.default or []))
            if d.value not in values["defaults.avoid"]:
                values["defaults.avoid"].append(d.value)
        else:
            values[d.key] = d.value
    if values:
        write_config_values(path, values)
        load(wd.root, cwd)
    return path, picked
