"""Does the request text actually authorize deletion?

Clear cases are decided by multilingual keyword rules (free, deterministic). Anything the
rules cannot settle - or where deletion words and keep/non-deletion words both appear - is
sent to Claude once and cached.
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPT = (Path(__file__).resolve().parent / "prompts" / "intent_classifier.md").read_text(encoding="utf-8")
SCHEMA = {
    "type": "object",
    "properties": {
        "authorizes_deletion": {"type": "boolean"},
        "request_type": {"type": "string"},
        "language": {"type": "string"},
    },
    "required": ["authorizes_deletion", "request_type", "language"],
    "additionalProperties": False,
}

DELETE_WORDS = re.compile(
    r"delet|eras(e|ure)|close (my|this|the) account|account[- ]deletion|remove (my|the) (account|profile)"
    r"|suppression|supprim|eliminaci|eliminar|exclus[ãa]o|excluir|löschung|löschen|cancell?a"
    r"|削除|삭제|删除|注销", re.I)
KEEP_WORDS = re.compile(
    r"leave (the|my) (account|profile)|keep (the|my) account|do not close|don't close|without removing"
    r"|continue using|marketing lists? only|promotional emails|unsubscribe|opt[- ]out"
    r"|\b(need|want|send me|get|request) a copy of|download my|what personal information|misspell|out of date|reset(ting)? the login"
    r"|change the email|correct it", re.I)


class IntentClassifier:
    def __init__(self, llm):
        self.llm = llm
        self.decisions: dict[str, str] = {}

    def rule(self, text: str) -> bool | None:
        d, k = bool(DELETE_WORDS.search(text)), bool(KEEP_WORDS.search(text))
        if d and not k:
            return True
        if k and not d:
            return False
        return None

    def is_deletion(self, text: str) -> bool:
        decided = self.rule(text)
        if decided is not None:
            self.decisions[text] = "rule"
            return decided
        result = self.llm.json_call("intent_classification", PROMPT, f"Request text:\n{text}", SCHEMA,
                                    max_tokens=2000)
        if result is not None:
            self.decisions[text] = "llm"
            return bool(result["authorizes_deletion"])
        # No model reachable: an explicit keep/opt-out wording wins over a bare deletion word.
        self.decisions[text] = "fallback"
        return not bool(KEEP_WORDS.search(text))
