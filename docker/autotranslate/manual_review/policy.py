"""Shared manual-review policy used by inspection and AI triage."""

APPROVABLE_CUE_RULES = frozenset({
    "copied_source",
    "ambiguous_copied_source",
    "unexpected_script",
    "excessive_lines",
    "cue_too_long",
    "abnormal_expansion",
    "garbage",
    "prompt_marker",
})
