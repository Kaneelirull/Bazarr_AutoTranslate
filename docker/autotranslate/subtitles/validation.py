"""Validation compatibility exports."""

from clean_et_subs import (
    ValidationIssue,
    ValidationReport,
    evaluate_subtitle_completeness,
    validate_cue_pair,
    validate_subtitle_pair,
    validate_subtitle_without_source,
)

__all__ = [
    "ValidationIssue", "ValidationReport", "evaluate_subtitle_completeness",
    "validate_cue_pair", "validate_subtitle_pair", "validate_subtitle_without_source",
]
