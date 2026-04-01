"""
Shared helpers for anomaly confidence scoring.

Formula: confidence = min(1.0, max(0.0, (|z| - 2) / 3))

The z-score represents how many standard deviations away from the mean a value
is.  Values below 2σ always yield 0.0 (low confidence); values at or above 5σ
saturate at 1.0 (high confidence).
"""

from __future__ import annotations


def compute_confidence(z_abs: float) -> float:
    """Return a confidence score in [0, 1] from an absolute z-score.

    Args:
        z_abs: Absolute value of the z-score (non-negative float).

    Returns:
        Confidence in the range [0.0, 1.0].  Values below 2 return 0.0;
        values at 5 or above return 1.0.
    """
    return min(1.0, max(0.0, (z_abs - 2) / 3))


def severity_label(confidence: float) -> str:
    """Map a confidence score to a human-readable severity label.

    Args:
        confidence: Float in [0.0, 1.0] as returned by :func:`compute_confidence`.

    Returns:
        ``"low"`` for 0.0–0.33, ``"medium"`` for 0.34–0.66, ``"high"`` for
        0.67–1.0.
    """
    if confidence <= 0.33:
        return "low"
    elif confidence <= 0.66:
        return "medium"
    return "high"
