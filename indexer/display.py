"""
indexer/display.py — shared terminal display helpers for indexer output.
"""

from __future__ import annotations

_GRADE_COLOUR = {
    "S":  "\033[95m",   # magenta
    "A+": "\033[92m",   # bright green
    "A":  "\033[92m",
    "A-": "\033[92m",
    "B+": "\033[93m",   # yellow
    "B":  "\033[93m",
    "B-": "\033[93m",
}
_RESET = "\033[0m"
_DIM   = "\033[2m"


def role_label(role_signals: list[str]) -> str:
    mapping = {
        "ml_engineer_signal": "ML",
        "devops_signal":      "DevOps",
        "fullstack_signal":   "Full-Stack",
        "backend_signal":     "Backend",
        "fde_signal":         "FDE",
    }
    labels = [mapping[s] for s in role_signals if s in mapping]
    return ", ".join(labels) if labels else "—"


def log_accepted(profile: dict, grade: str, score: float) -> None:
    p = profile.get("profile", {})
    name     = (p.get("name") or p.get("login", "?")).ljust(28)[:28]
    login    = ("@" + p.get("login", "")).ljust(22)[:22]
    location = (p.get("location") or "").ljust(20)[:20]
    role     = role_label(profile.get("role_signals") or []).ljust(12)[:12]
    langs    = ", ".join(l["name"] for l in (profile.get("languages") or [])[:3]).ljust(24)[:24]
    email_flag    = "📧" if p.get("email") else "  "
    linkedin_flag = "🔗" if "linkedin.com" in (p.get("websiteUrl") or "") else "  "

    colour = _GRADE_COLOUR.get(grade, "")
    print(
        f"  {colour}{grade:>2}{_RESET}  {score:5.1f}  "
        f"{name}  {login}  {_DIM}{location}{_RESET}  "
        f"{role}  {_DIM}{langs}{_RESET}  {email_flag}{linkedin_flag}",
        flush=True,
    )


def print_section_header(label: str) -> None:
    print(f"\n{'─'*100}", flush=True)
    print(f"  {label}", flush=True)
    print(f"  {'Grade':>4}  {'Score':>5}  {'Name':<28}  {'Login':<22}  {'Location':<20}  {'Role':<12}  {'Top Languages':<24}", flush=True)
    print(f"{'─'*100}", flush=True)
