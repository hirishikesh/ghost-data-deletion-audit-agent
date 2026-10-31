"""Thin Claude client with a disk cache and a token/cost ledger.

Every call is keyed by (model, prompt, payload hash). A cached answer costs nothing and
is recorded as a cache hit, so re-runs are free and deterministic. The API key is read
from the environment (ANTHROPIC_API_KEY) or a local, uncommitted .env file.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path

MODEL = os.environ.get("AUDIT_MODEL", "claude-opus-5-5")
# USD per million tokens (input, output) - Anthropic first-party list prices.
PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00),
          "claude-haiku-5-5": (0.10, 0.50)}


def _load_dotenv(root: Path):
    env = root / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


class LLMClient:
    def __init__(self, cache_dir: Path, offline: bool = False, model: str = MODEL,
                 env_root: Path | None = None):
        _load_dotenv(env_root or Path.cwd())
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = cache_dir / "llm_cache.json"
        self.ledger_file = cache_dir / "failed_calls.jsonl"   # billed calls with no usable answer
        self.cache = json.loads(self.cache_file.read_text(encoding="utf-8")) if self.cache_file.exists() else {}
        self.model = model
        self.offline = offline
        self.budget = float(os.environ.get("AUDIT_BUDGET_USD", "3.00"))
        self._client = None
        # per (model, purpose): calls, cache_hits, input_tokens, output_tokens
        self.usage = defaultdict(lambda: {"calls": 0, "cache_hits": 0, "input_tokens": 0,
                                          "output_tokens": 0})

    @property
    def available(self) -> bool:
        return not self.offline and bool(os.environ.get("ANTHROPIC_API_KEY")
                                         or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def json_call(self, purpose: str, system: str, text: str, schema: dict,
                  image_path: Path | None = None, max_tokens: int = 4000) -> dict | None:
        """Structured-output call. Returns parsed JSON, or None when no model is reachable."""
        payload = text
        if image_path is not None:
            payload += hashlib.sha256(image_path.read_bytes()).hexdigest()
        key = hashlib.sha256(json.dumps([self.model, system, payload, schema],
                                        sort_keys=True).encode("utf-8")).hexdigest()
        stats = self.usage[(self.model, purpose)]
        if key in self.cache:
            stats["cache_hits"] += 1
            return self.cache[key]["result"]
        if not self.available:
            return None
        # Spend guard: every API call is cached, so the cache holds total spend to date.
        worst_case = self.cost(self.model, 4000 if image_path else 1500, max_tokens)
        if self.spent() + worst_case > self.budget:
            stats["skipped_budget"] = stats.get("skipped_budget", 0) + 1
            return None
        content = []
        if image_path is not None:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.standard_b64encode(image_path.read_bytes()).decode("ascii")}})
        content.append({"type": "text", "text": text})
        import anthropic
        try:
            response = self._create(system, content, schema, max_tokens)
        except anthropic.APIError as exc:
            # Rate limit, network, 5xx after the SDK's own retries: this evidence stays
            # unread (and is reported as a gap) instead of aborting the whole audit.
            self._record(purpose, self.model, 0, 0, f"api_error:{type(exc).__name__}")
            return None
        served = getattr(response, "model", None) or self.model
        usage = response.usage
        if response.stop_reason in ("refusal", "max_tokens"):
            # Billed but unusable - count it, do not cache it.
            self._record(purpose, served, usage.input_tokens, usage.output_tokens, response.stop_reason)
            return None
        try:
            out = next(b.text for b in response.content if b.type == "text")
            result = json.loads(out)
        except (StopIteration, json.JSONDecodeError):
            self._record(purpose, served, usage.input_tokens, usage.output_tokens, "unparseable")
            return None
        self.usage[(served, purpose)]["calls"] += 1
        self.usage[(served, purpose)]["input_tokens"] += usage.input_tokens
        self.usage[(served, purpose)]["output_tokens"] += usage.output_tokens
        self.cache[key] = {"purpose": purpose, "model": served, "result": result,
                           "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        self._save()
        return result

    def _record(self, purpose: str, model: str, inp: int, out: int, outcome: str):
        """Log a call that produced no usable answer. It still counts toward spend."""
        stats = self.usage[(model, purpose)]
        stats["failed"] = stats.get("failed", 0) + 1
        stats["input_tokens"] += inp
        stats["output_tokens"] += out
        entry = {"purpose": purpose, "model": model, "input_tokens": inp,
                 "output_tokens": out, "outcome": outcome}
        with open(self.ledger_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def _create(self, system, content, schema, max_tokens):
        import anthropic
        params = dict(model=self.model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": content}],
                      output_config={"effort": "low",
                                     "format": {"type": "json_schema", "schema": schema}})
        try:
            # Server-side fallback re-runs a declined request on another model in the same call.
            return self.client().beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        except (anthropic.BadRequestError, TypeError):
            # Fallbacks unsupported by this SDK version or account: plain request.
            return self.client().messages.create(**params)

    def _save(self):
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, indent=1, sort_keys=True, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.cache_file)

    # ------------------------------------------------------------ usage
    def cost(self, model, inp, out) -> float:
        # An unknown model is priced at the most expensive known rate, never at zero.
        pin, pout = PRICES.get(model, max(PRICES.values()))
        return inp / 1e6 * pin + out / 1e6 * pout

    def spent(self) -> float:
        """Total USD of every API call made so far: cached answers plus billed failures."""
        total = sum(self.cost(e["model"], e["input_tokens"], e["output_tokens"])
                    for e in self.cache.values())
        if self.ledger_file.exists():
            for line in self.ledger_file.read_text(encoding="utf-8").splitlines():
                e = json.loads(line)
                total += self.cost(e["model"], e["input_tokens"], e["output_tokens"])
        return total

    def write_usage(self, path: Path, requests: int):
        """Usage of this run, plus what the cached answers originally cost to produce."""
        produced = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        for entry in self.cache.values():
            p = produced[(entry["model"], entry["purpose"])]
            p["calls"] += 1
            p["input_tokens"] += entry["input_tokens"]
            p["output_tokens"] += entry["output_tokens"]
        data = {
            "requests": requests,
            "this_run": [{"model": m, "purpose": p, **v,
                          "cost_usd": round(self.cost(m, v["input_tokens"], v["output_tokens"]), 6)}
                         for (m, p), v in sorted(self.usage.items())],
            "cache_build": [{"model": m, "purpose": p, **v,
                             "cost_usd": round(self.cost(m, v["input_tokens"], v["output_tokens"]), 6)}
                            for (m, p), v in sorted(produced.items())],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def usage_summary(self, requests: int) -> str:
        lines = []
        for (m, p), v in sorted(self.usage.items()):
            c = self.cost(m, v["input_tokens"], v["output_tokens"])
            lines.append(f"  {m} [{p}] calls={v['calls']} cache_hits={v['cache_hits']} "
                         f"in={v['input_tokens']} out={v['output_tokens']} cost=${c:.4f}")
        return "model usage this run:\n" + ("\n".join(lines) if lines else "  none")
