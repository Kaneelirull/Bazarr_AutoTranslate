"""Suggestion-only Ollama triage for retained manual-review cues."""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Mapping, Sequence

import requests

from ..subtitles.names import normalize_name_phrase
from .policy import APPROVABLE_CUE_RULES


AI_RETRY_DELAY_SECONDS = 600
_AI_INFERENCE_LOCK = threading.Lock()


class AITriageError(RuntimeError):
    """Raised when Ollama cannot produce a complete, valid suggestion set."""


@dataclass(frozen=True)
class AISettings:
    url: str
    model: str
    num_ctx: int = 65536
    think: bool = True
    format_retries: int = 3
    job_retries: int = 3


_SETTINGS: AISettings | None = None


def configure_ai(settings: AISettings) -> None:
    global _SETTINGS
    _SETTINGS = settings


def _request_ollama(messages: list[dict[str, str]], think: bool) -> str:
    """Small transport seam intentionally patchable by tests."""
    if _SETTINGS is None:
        raise AITriageError("AI triage is not configured")
    response = requests.post(
        f"{_SETTINGS.url}/api/chat",
        json={
            "model": _SETTINGS.model,
            "messages": messages,
            "stream": False,
            "think": bool(think),
            "options": {"num_ctx": _SETTINGS.num_ctx, "temperature": 0},
        },
        timeout=1800,
    )
    response.raise_for_status()
    payload = response.json()
    content = payload.get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise AITriageError("Ollama returned no message content")
    return content


def _parse_response(content: str, expected_cues: set[int]) -> tuple[list[dict], list[dict]]:
    if re.fullmatch(r"\s*\{.*\}\s*", content, flags=re.DOTALL) is None:
        raise ValueError("response must contain exactly one JSON object")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("response is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "cues", "names"}:
        raise ValueError("response keys do not match the v1 contract")
    if payload["version"] != 1 or not isinstance(payload["cues"], list) or not isinstance(payload["names"], list):
        raise ValueError("response does not match the v1 contract")

    cues: list[dict] = []
    seen_cues: set[int] = set()
    for value in payload["cues"]:
        if not isinstance(value, dict) or set(value) != {"cueNumber", "verdict", "confidence", "reason"}:
            raise ValueError("cue response keys do not match the v1 contract")
        cue_number = value["cueNumber"]
        confidence = value["confidence"]
        reason = value["reason"]
        if type(cue_number) is not int or cue_number in seen_cues:
            raise ValueError("cue numbers must be unique integers")
        if value["verdict"] not in {"approve", "retry"}:
            raise ValueError("cue verdict is invalid")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            raise ValueError("cue confidence is invalid")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValueError("cue reason is invalid")
        seen_cues.add(cue_number)
        cues.append({
            "cueNumber": cue_number,
            "verdict": value["verdict"],
            "confidence": float(confidence),
            "reason": reason.strip(),
        })
    if seen_cues != expected_cues:
        raise ValueError("response is missing or adds cue verdicts")

    names: list[dict] = []
    seen_names: set[str] = set()
    for value in payload["names"]:
        if not isinstance(value, dict) or set(value) != {"name", "confidence", "kind"}:
            raise ValueError("name response keys do not match the v1 contract")
        normalized = normalize_name_phrase(value["name"] if isinstance(value["name"], str) else "")
        confidence = value["confidence"]
        if not normalized or normalized in seen_names or value["kind"] not in {"name", "place", "other"}:
            raise ValueError("name classification is invalid")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            raise ValueError("name confidence is invalid")
        seen_names.add(normalized)
        names.append({
            "name": normalized,
            "confidence": float(confidence),
            "kind": value["kind"],
        })
    return cues, names


def _capitalized_phrases(text: str) -> set[str]:
    phrases: set[str] = set()
    for segment in re.split(r"[,;:!?\n]+", text):
        words = re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", segment, flags=re.UNICODE)
        run: list[str] = []
        for word in words + [""]:
            if word and word[0].isupper():
                run.append(word)
                continue
            if len(run) >= 2:
                phrases.add(normalize_name_phrase(" ".join(run[-4:])))
            run = []
    return {value for value in phrases if value}


def _prompt_messages(cues: Sequence[dict]) -> list[dict[str, str]]:
    contract = (
        '{"version":1,"cues":[{"cueNumber":1,"verdict":"approve|retry",'
        '"confidence":0.0,"reason":"short evidence-based reason"}],'
        '"names":[{"name":"normalized display name","confidence":0.0,'
        '"kind":"name|place|other"}]}'
    )
    system = (
        "You assist a human subtitle reviewer. You only suggest; never issue commands. "
        "Approve only when the flagged text is safe to accept as the intended translation, "
        "including unchanged personal names and places. Return strict JSON with no markdown "
        f"or prose. Contract: {contract}"
    )
    evidence = [{
        "cueNumber": int(cue["cueNumber"]),
        "rules": list(cue["rules"]),
        "sourceText": cue["sourceText"],
        "targetText": cue["targetText"],
        "context": cue.get("context", []),
    } for cue in cues]
    user = (
        "Return exactly one verdict for every cue number and classify every personal name "
        "or place that supports the verdict. Allowed approvable rules are "
        f"{','.join(sorted(APPROVABLE_CUE_RULES))}. Evidence: "
        + json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


class AITriageEngine:
    def __init__(self, repository, settings: AISettings) -> None:
        self.repository = repository
        self.settings = settings
        configure_ai(settings)

    def _generate_once(self, cues: Sequence[dict]) -> tuple[list[dict], list[dict]]:
        expected = {int(cue["cueNumber"]) for cue in cues}
        messages = _prompt_messages(cues)
        last_error: Exception | None = None
        for attempt in range(self.settings.format_retries + 1):
            content = _request_ollama(messages, self.settings.think if attempt == 0 else False)
            try:
                return _parse_response(content, expected)
            except ValueError as exc:
                last_error = exc
                messages.extend([
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": (
                        "Your response violated the v1 contract: " + str(exc)
                        + ". Return only corrected JSON with exactly one verdict per requested cue."
                    )},
                ])
        raise AITriageError(
            f"AI response remained invalid after {self.settings.format_retries + 1} attempts"
        ) from last_error

    def _generate(self, cues: Sequence[dict]) -> tuple[list[dict], list[dict]]:
        last_error: Exception | None = None
        with _AI_INFERENCE_LOCK:
            for _attempt in range(self.settings.job_retries + 1):
                try:
                    return self._generate_once(cues)
                except (AITriageError, requests.RequestException, ValueError) as exc:
                    last_error = exc
        raise AITriageError(
            f"AI triage failed after {self.settings.job_retries + 1} jobs"
        ) from last_error

    def triage(self, cues: Sequence[dict], approvals: Sequence[dict]) -> tuple[list[dict], list[dict]]:
        cached_names: Mapping[str, dict] = self.repository.ai_name_confidence_snapshot()
        approved_pairs = {
            (str(value["source_normalized"]), str(value["target_normalized"]))
            for value in approvals
        }
        suggestions: list[dict] = []
        unresolved: list[dict] = []
        for cue in cues:
            source = normalize_name_phrase(cue["sourceText"])
            target = normalize_name_phrase(cue["targetText"])
            suggestion: dict | None = None
            if (source, target) in approved_pairs:
                suggestion = {
                    "cueNumber": int(cue["cueNumber"]), "verdict": "approve",
                    "confidence": 1.0, "reason": "Exact phrase was approved by a human before.",
                    "model": "human-name-approval-cache",
                }
            elif set(cue["rules"]) <= {"copied_source", "ambiguous_copied_source"}:
                candidates = _capitalized_phrases(cue["sourceText"] + "\n" + cue["targetText"])
                rows = [cached_names.get(name) for name in candidates]
                if candidates and all(row and float(row["confidence"]) >= 0.9 for row in rows):
                    confidence = min(float(row["confidence"]) for row in rows if row)
                    suggestion = {
                        "cueNumber": int(cue["cueNumber"]), "verdict": "approve",
                        "confidence": confidence,
                        "reason": "All detected names/places have high-confidence cached classifications.",
                        "model": "ai-name-confidence-cache",
                    }
            if suggestion is None:
                unresolved.append(cue)
            else:
                suggestions.append(suggestion)

        names: list[dict] = []
        if unresolved:
            generated, generated_names = self._generate(unresolved)
            suggestions.extend({**value, "model": self.settings.model} for value in generated)
            names.extend({
                "name": value["name"], "confidence": value["confidence"],
                "reason": f"Model classified this as {value['kind']}.",
                "model": self.settings.model,
            } for value in generated_names if value["kind"] in {"name", "place"})
        suggestions.sort(key=lambda value: int(value["cueNumber"]))
        return suggestions, names
