from __future__ import annotations


class KumimasuError(Exception):
    pass


class StepError(KumimasuError):
    pass


class LLMError(KumimasuError):
    pass


class ConfigError(KumimasuError):
    pass


class TaggedOutputError(LLMError):
    pass
