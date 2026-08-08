"""Alt-account risk scoring.

What this is not: Double Counter's actual detection. That works by redirecting
joiners to a hosted web page and fingerprinting the IP and browser. Discord
never gives a bot a member's IP address, and hosting a public page is exactly
what the no-dashboard constraint rules out. Anything claiming otherwise from
inside a self-hosted bot is guessing.

What this is: the strongest signals actually available to a bot, weighted and
combined into a 0-100 score with every contribution named. A score is evidence
for a human to look at, not a verdict -- which is why ``?altcheck`` prints the
breakdown rather than a yes or no.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

#: Score at or above which a join is worth a human looking at it.
DEFAULT_FLAG_THRESHOLD = 55

#: Maximum score. Contributions are summed and then clamped.
MAX_SCORE = 100


@dataclass(slots=True)
class RiskResult:
    """A score plus the reasons behind it."""

    score: int
    reasons: list[str] = field(default_factory=list)

    @property
    def level(self) -> str:
        if self.score >= 80:
            return "high"
        if self.score >= 55:
            return "elevated"
        if self.score >= 30:
            return "low"
        return "minimal"


def name_similarity(name: str, others: list[str]) -> tuple[float, str]:
    """Closest match between a name and a list of existing names.

    Raids frequently use near-identical usernames. Comparing against members who
    are already here catches that pattern without needing any history.

    Returns:
        ``(ratio, closest_name)``. Ratio is 0.0 to 1.0.
    """
    best = 0.0
    closest = ""
    lowered = name.lower()

    for other in others:
        if other.lower() == lowered:
            # An exact match is almost always the member themselves in the list.
            continue
        ratio = SequenceMatcher(None, lowered, other.lower()).ratio()
        if ratio > best:
            best, closest = ratio, other

    return best, closest


def score_account(
    *,
    account_age_seconds: int,
    has_default_avatar: bool,
    similarity: float = 0.0,
    similar_to: str = "",
    join_burst: int = 0,
    invite_known: bool = True,
    has_flags: bool = False,
    name_is_generic: bool = False,
) -> RiskResult:
    """Combine the available signals into a 0-100 risk score.

    Args:
        account_age_seconds: How old the Discord account is.
        has_default_avatar: No avatar has ever been set.
        similarity: Closest username match against existing members, 0.0 to 1.0.
        similar_to: The name that matched, for the breakdown.
        join_burst: How many other accounts joined in the same short window.
        invite_known: Whether the invite used could be identified.
        has_flags: Whether Discord itself marks the account (spammer, quarantined).
        name_is_generic: Username is the ``name1234`` shape Discord auto-suggests.

    Returns:
        The score and a human-readable reason for each contribution. No single
        signal can reach the threshold alone -- account age is the strongest at
        35 points, and a new account with nothing else odd about it is common and
        innocent.
    """
    score = 0
    reasons: list[str] = []

    days = account_age_seconds / 86400

    if days < 1:
        score += 35
        reasons.append("Account is less than 24 hours old (+35)")
    elif days < 7:
        score += 25
        reasons.append(f"Account is {days:.0f} days old (+25)")
    elif days < 30:
        score += 15
        reasons.append(f"Account is {days:.0f} days old (+15)")
    elif days < 90:
        score += 5
        reasons.append(f"Account is {days:.0f} days old (+5)")

    if has_default_avatar:
        score += 15
        reasons.append("No avatar set (+15)")

    if similarity >= 0.9:
        score += 25
        reasons.append(f"Username nearly identical to {similar_to!r} (+25)")
    elif similarity >= 0.75:
        score += 15
        reasons.append(f"Username very similar to {similar_to!r} (+15)")
    elif similarity >= 0.6:
        score += 7
        reasons.append(f"Username similar to {similar_to!r} (+7)")

    if join_burst >= 10:
        score += 25
        reasons.append(f"Joined alongside {join_burst} other accounts (+25)")
    elif join_burst >= 5:
        score += 15
        reasons.append(f"Joined alongside {join_burst} other accounts (+15)")
    elif join_burst >= 3:
        score += 8
        reasons.append(f"Joined alongside {join_burst} other accounts (+8)")

    if not invite_known:
        score += 10
        reasons.append("Invite used could not be identified (+10)")

    if has_flags:
        score += 30
        reasons.append("Discord has flagged this account (+30)")

    if name_is_generic:
        score += 10
        reasons.append("Username is the auto-generated name+digits shape (+10)")

    if not reasons:
        reasons.append("No risk signals found")

    return RiskResult(score=min(score, MAX_SCORE), reasons=reasons)


def looks_auto_generated(name: str) -> bool:
    """Whether a username looks like Discord's own suggestion.

    Discord offers ``word1234`` when a name is taken, and throwaway accounts
    frequently keep it. On its own this means very little, which is why it is
    worth ten points rather than forty.
    """
    if len(name) < 5:
        return False
    tail = name[-4:]
    return tail.isdigit() and name[:-4].isalpha()
