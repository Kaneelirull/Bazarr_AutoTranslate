from __future__ import annotations

from dataclasses import dataclass
import re


COPIED_PROSE = "copied_prose"
LIKELY_INVARIANT = "likely_invariant"
AMBIGUOUS = "ambiguous"
REVIEW_REQUIRED = "review_required"

_TOKEN_RE = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b\S+\.(?:com|org|net|io|tv)\b")
_URL_ONLY_RE = re.compile(
    r"(?i)^(?:https?://|www\.)\S+$|^\S+\.(?:com|org|net|io|tv)(?:/\S*)?$"
)
_CONNECTORS = {"and", "or", "vs", "versus"}
_ARTICLES = {"a", "an", "the"}
_NAME_PARTICLES = {"van", "von", "de", "di", "da", "la", "le", "of"}
_ENGLISH_PROSE_MARKERS = {
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "us", "them", "this", "that", "these", "those", "am", "is", "are",
    "was", "were", "be", "been", "being", "have", "has", "had", "do",
    "does", "did", "can", "cannot", "could", "will", "would", "shall",
    "should", "may", "might", "must", "not", "if", "then", "than",
    "because", "for", "from", "in", "into", "on", "to", "with",
    "without", "here", "there", "what", "when", "where", "who", "why",
    "how",
}


@dataclass(frozen=True)
class CopiedSourceAssessment:
    outcome: str
    reason: str
    evidence: dict


def _alpha_count(token: str) -> int:
    return sum(character.isalpha() for character in token)


def _is_model_marker(token: str) -> bool:
    return any(character.isdigit() for character in token) or (
        _alpha_count(token) >= 2 and token.isupper()
    )


def _is_entity_token(token: str) -> bool:
    return bool(token) and (
        _is_model_marker(token)
        or (token[0].isupper() and not token[1:].isupper())
    )


def _shape(
    text: str,
    tokens: list[str],
    *,
    has_url: bool,
    model_markers: int,
    has_connector: bool,
    has_groups: bool,
) -> str:
    if has_url:
        return "url"
    if any(any(character.isdigit() for character in token) for token in tokens):
        return "model"
    if model_markers:
        return "acronym"
    if has_groups:
        return "grouped_name"
    if has_connector:
        return "connector_name"
    if tokens and all(
        _is_entity_token(token)
        or token.casefold() in _ARTICLES | _NAME_PARTICLES | _CONNECTORS
        for token in tokens
    ):
        return "title_case"
    return "mixed"


def assess_copied_source(
    source: str,
    target: str,
    *,
    source_normalized: str,
    target_normalized: str,
    similarity: float,
    repair_eligible: bool,
    cue_language: str | None = None,
    cue_language_confidence: float | None = None,
    whole_target_confidence: float | None = None,
    context_confidence: float | None = None,
) -> CopiedSourceAssessment | None:
    """Classify a copied cue without returning or persisting subtitle text."""
    tokens = _TOKEN_RE.findall(re.sub(r"<[^>]+>", " ", source))
    folded = [token.casefold() for token in tokens]
    exact = bool(source_normalized) and source_normalized == target_normalized
    has_url = bool(_URL_RE.search(source))
    url_only = bool(_URL_ONLY_RE.fullmatch(source.strip()))
    model_markers = sum(_is_model_marker(token) for token in tokens)
    has_connector = bool(re.search(r"[&/|]", source)) or any(
        token in _CONNECTORS for token in folded
    )
    groups = [
        _TOKEN_RE.findall(group)
        for group in re.split(r"[,;:!]+", source)
        if group.strip()
    ]
    has_groups = len(groups) >= 2 and all(1 <= len(group) <= 3 for group in groups)

    leading_article = bool(folded and folded[0] in _ARTICLES)
    content_tokens = [
        token for index, token in enumerate(tokens)
        if token.casefold() not in _CONNECTORS | _NAME_PARTICLES
        and not (index == 0 and token.casefold() in _ARTICLES)
    ]
    entity_shaped = bool(content_tokens) and all(
        _is_entity_token(token) for token in content_tokens
    )
    expanded_candidate = exact and 2 <= len(content_tokens) <= 5 and entity_shaped
    allowed_structure = bool(tokens) and all(
        _is_entity_token(token)
        or token.casefold() in _CONNECTORS | _NAME_PARTICLES
        or (index == 0 and token.casefold() in _ARTICLES)
        for index, token in enumerate(tokens)
    )
    objective_invariant = url_only or (
        entity_shaped
        and allowed_structure
        and (bool(model_markers) or has_connector or has_groups)
    )

    token_shape = _shape(
        source,
        tokens,
        has_url=has_url,
        model_markers=model_markers,
        has_connector=has_connector,
        has_groups=has_groups,
    )
    evidence = {
        "similarity": round(float(similarity), 3),
        "exactNormalizedCopy": exact,
        "tokenCount": len(tokens),
        "tokenShape": token_shape,
        "modelMarkerCount": model_markers,
        "cueLanguage": cue_language,
        "cueLanguageConfidence": (
            round(float(cue_language_confidence), 3)
            if cue_language_confidence is not None else None
        ),
        "wholeTargetConfidence": (
            round(float(whole_target_confidence), 3)
            if whole_target_confidence is not None else None
        ),
        "contextConfidence": (
            round(float(context_confidence), 3)
            if context_confidence is not None else None
        ),
    }

    prose_markers = [
        token for index, token in enumerate(folded)
        if token in _ENGLISH_PROSE_MARKERS
        and token not in _NAME_PARTICLES
        and not (index == 0 and token in _ARTICLES and leading_article)
    ]
    if repair_eligible and prose_markers:
        return CopiedSourceAssessment(
            COPIED_PROSE,
            "Copy contains English prose markers.",
            evidence,
        )
    # A spelling is objective evidence only if its letters agree with the name.
    spelling = re.search(r"\b([A-Za-z](?:[- ][A-Za-z]){2,})[.!?]*$", target.strip())
    if exact and spelling:
        letters = re.sub(r"[^A-Za-z]", "", spelling.group(1)).casefold()
        prefix = _TOKEN_RE.findall(target[:spelling.start()])
        if prefix and prefix[-1].casefold() == letters:
            return CopiedSourceAssessment(LIKELY_INVARIANT, "Name agrees with its letter-by-letter spelling.", evidence)
    target_tokens = _TOKEN_RE.findall(re.sub(r"<[^>]+>", " ", target))
    target_entities = target_tokens and all(
        _is_entity_token(token) or token.casefold() in _NAME_PARTICLES for token in target_tokens
    )
    if repair_eligible and exact and not entity_shaped and 2 <= len(target_tokens) <= 6 and target_entities:
        return CopiedSourceAssessment(REVIEW_REQUIRED, "Possible unchanged name needs operator confirmation.", evidence)
    if expanded_candidate:
        if objective_invariant:
            return CopiedSourceAssessment(
                LIKELY_INVARIANT,
                "Copy retained because its structure indicates an invariant name or model.",
                evidence,
            )
        return CopiedSourceAssessment(
            REVIEW_REQUIRED if repair_eligible else AMBIGUOUS,
            "Possible unchanged name; review required when the copied-cue threshold is met.",
            evidence,
        )
    if objective_invariant and repair_eligible:
        return CopiedSourceAssessment(
            LIKELY_INVARIANT,
            "Copy retained because its structure indicates an invariant name or model.",
            evidence,
        )
    if repair_eligible:
        return CopiedSourceAssessment(
            COPIED_PROSE,
            "Copy does not have sufficient invariant structure.",
            evidence,
        )
    return None
