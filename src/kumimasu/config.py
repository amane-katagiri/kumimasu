"""Highest first: command-line options > the work directory's project.yaml `config:` > ./kumimasu.yaml in the current
directory > the user file ($KUMIMASU_CONFIG, else ~/.config/kumimasu/config.yaml) > the packaged defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

ENV = "KUMIMASU_CONFIG"
USER_FILE = Path("~/.config/kumimasu/config.yaml")
PROJECT_FILE = "kumimasu.yaml"
ROLES = ("writer", "baseline", "researcher", "judge", "interviewer", "designer", "detector", "rewriter", "auto")
KEYS: dict[str, type] = {**{f"providers.{r}": str for r in ROLES},
                         "surface.runs": int, "surface.min_votes": int, "surface.max_rounds": int,
                         "cache_dir": str, "rules_file": str, "serve.port": int, "serve.poll_seconds": float,
                         "workdir_root": str,
                         "defaults.register": str, "defaults.drop_list": str, "defaults.avoid": list,
                         "defaults.noise.skip_max": int, "defaults.noise.aside_max": int, "defaults.forms": str,
                         "interview.always_ask": list}
CHOICES = {"defaults.register": ("keitai", "joutai"), "defaults.drop_list": ("topics", "full", "none")}
PATH_KEYS = ("cache_dir", "rules_file", "workdir_root")
TRUST_ENV = "KUMIMASU_TRUST_PROJECT"
UNTRUSTED_LAYERS = ("workdir", "project")


def _flatten(data: Any, where: str, prefix: str = "") -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: {prefix or 'the top level'} must be a mapping")
    out: dict[str, Any] = {}
    for k, v in data.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and key not in KEYS:
            out |= _flatten(v, where, key + ".")
        elif key in KEYS:
            out[key] = v
        else:
            raise ConfigError(f"{where}: unknown key {key!r} (known: {', '.join(KEYS)})")
    return out


def _coerce(key: str, value: Any, where: str, base: Path | None) -> Any:
    if value is None:
        return None
    want = KEYS[key]
    if want is list:
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ConfigError(f"{where}: {key} must be a list of strings")
        return [x.strip() for x in value if x.strip()]
    try:
        value = want(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where}: {key} must be {want.__name__}") from e
    if key in CHOICES and value not in CHOICES[key]:
        raise ConfigError(f"{where}: {key} must be one of {', '.join(CHOICES[key])}")
    if key in PATH_KEYS:
        p = Path(value).expanduser()
        if not p.is_absolute() and base is not None:
            p = base / p
        return str(p.resolve()) if p.is_absolute() else str(p)
    return value


@dataclass
class Layer:
    name: str
    path: str
    values: dict[str, Any]
    ignored: list[str] = field(default_factory=list)


def sensitive(key: str, raw: Any) -> bool:
    if key.startswith("providers.") or key in ("cache_dir", "workdir_root"):
        return True
    if key == "rules_file" and isinstance(raw, str):
        p = Path(raw)
        return raw.startswith("~") or p.is_absolute() or ".." in p.parts
    return False


def _layer(name: str, where: str, flat: dict[str, Any], base: Path | None, trust: bool) -> Layer:
    ignored = [] if trust or name not in UNTRUSTED_LAYERS else [k for k, v in flat.items() if sensitive(k, v)]
    return Layer(name, where, {k: _coerce(k, v, where, base) for k, v in flat.items() if k not in ignored}, ignored)


@dataclass
class Config:
    layers: list[Layer] = field(default_factory=list)

    def _pick(self, key: str) -> tuple[Any, Layer | None]:
        for layer in self.layers:
            if key in layer.values and layer.values[key] is not None:
                return layer.values[key], layer
        return None, None

    def get(self, key: str, override: Any = None) -> Any:
        if override is not None:
            return override
        return self._pick(key)[0]

    def provider(self, role: str, override: str | None = None) -> str:
        return self.get(f"providers.{role}", override)

    def source(self, key: str) -> str:
        layer = self._pick(key)[1]
        return f"{layer.name} ({layer.path})" if layer else "-"

    @property
    def cache_dir(self) -> str:
        return self.get("cache_dir")

    @property
    def workdir_root(self) -> str:
        return self.get("workdir_root")

    def warnings(self) -> list[str]:
        return [f"{layer.path} の {key} は使いません（このフォルダや作業ディレクトリの設定は、LLM の呼び方・キャッシュ・"
                f"作業場所・外のルールファイルを変えられません。信頼するなら --trust-project か {TRUST_ENV}=1）"
                for layer in self.layers for key in layer.ignored]

    def rules_path(self) -> Path | None:
        value, layer = self._pick("rules_file")
        if value is None:
            return None
        p = Path(value)
        if p.exists():
            return p
        if layer is not None and layer.name == "default":
            return None
        raise ConfigError(f"rules_file {p} does not exist (set in {layer.name if layer else '?'})")

    def effective(self) -> list[dict]:
        rows = []
        for key in KEYS:
            value, layer = self._pick(key)
            rows.append({"key": key, "value": value, "layer": layer.name if layer else "-",
                         "file": layer.path if layer else "",
                         "ignored": [x.path for x in self.layers if key in x.ignored]})
        return rows


def user_path() -> Path:
    env = os.environ.get(ENV)
    return Path(env).expanduser() if env else USER_FILE.expanduser()


def _file_layer(name: str, path: Path, trust: bool) -> Layer | None:
    if not path.is_file():
        return None
    where = str(path)
    return _layer(name, where, _flatten(yaml.safe_load(path.read_text(encoding="utf-8")), where), path.resolve().parent,
                  trust)


def trusted_by_env() -> bool:
    return os.environ.get(TRUST_ENV, "") not in ("", "0")


def load(workdir: Path | None = None, cwd: Path | None = None, cli: dict[str, Any] | None = None,
         trust: bool = False) -> Config:
    trust = trust or trusted_by_env()
    layers: list[Layer] = []
    if cli:
        layers.append(Layer("cli", "command line", {k: v for k, v in cli.items() if v is not None}))
    if workdir is not None and (workdir / "project.yaml").is_file():
        data = yaml.safe_load((workdir / "project.yaml").read_text(encoding="utf-8")) or {}
        where = str(workdir / "project.yaml")
        layers.append(_layer("workdir", where, _flatten(data.get("config") or {}, where + " config"), workdir.resolve(),
                             trust))
    for name, path in (("project", (cwd or Path.cwd()) / PROJECT_FILE), ("user", user_path())):
        if (layer := _file_layer(name, path, trust)) is not None:
            layers.append(layer)
    text = resources.files("kumimasu").joinpath("default_config.yaml").read_text(encoding="utf-8")
    flat = _flatten(yaml.safe_load(text), "default_config.yaml")
    layers.append(Layer("default", "default_config.yaml", {k: _coerce(k, v, "default_config.yaml", None) for k, v in flat.items()}))
    return Config(layers)


TEMPLATE = """# kumimasu の設定。書いた値だけが下の層（ユーザー → 同梱の既定）より優先されます。
# 相対パスは、このファイルのあるディレクトリから解決します。
# ./kumimasu.yaml と作業ディレクトリの project.yaml の config: では、providers・cache_dir・workdir_root と、
# 絶対パスや .. を含む rules_file は、--trust-project（または KUMIMASU_TRUST_PROJECT=1）のときだけ使います。
#
# providers:              # 役割ごとの LLM（claude-cli:opus / claude-cli:sonnet / codex-cli / anthropic:... / fake）
#   writer: claude-cli:opus       # draft・revise（ツールなし）
#   baseline: claude-cli:opus     # mark のウェブ調査ありの一般的な記事（送るのは題と読者だけ）
#   researcher: claude-cli:sonnet # 下書きの前のウェブ調査（送るのは題・読者・ねらい・持ち帰り・調べることだけ）
#   judge: claude-cli:sonnet      # mark の網羅の判定、check の判定
#   interviewer: claude-cli:sonnet
#   designer: claude-cli:sonnet   # design・noise・review
#   detector: claude-cli:sonnet   # 表面の検出（メタ言説・つなぎの効用文）
#   rewriter: claude-cli:sonnet   # 最終チェックの書き直す
#   auto: claude-cli:sonnet       # auto
# surface:
#   runs: 3
#   min_votes: 2
#   max_rounds: 3
# cache_dir: ~/.cache/kumimasu
# rules_file: ~/.config/kumimasu/rules.yaml
# serve:
#   port: 8792
#   poll_seconds: 3
# workdir_root: ~/.cache/kumimasu/work   # 「一時的な場所」を選んだときの作業ディレクトリの置き場（700 で作る）
# defaults:                # 新しい設計の既定（kumimasu prefs diff / save で、記事での変更を書き戻せる）
#   register: keitai       # keitai（です・ます）/ joutai（だ・である）
#   drop_list: topics      # 書かない事柄の載せ方: topics / full / none
#   avoid: []              # どの記事でも書かない話題の名前
#   noise:
#     skip_max: 3
#     aside_max: 2
#   forms: ""              # 形の好み（例: 比較は表、手順は番号付きリスト）
# interview:
#   always_ask:            # どのインタビューにも足す質問
#     - 読者に一つだけ持ち帰ってほしいことは何ですか
"""


def init_template(path: Path) -> Path:
    if path.exists():
        raise ConfigError(f"{path} はすでにあります（上書きしません）")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path
