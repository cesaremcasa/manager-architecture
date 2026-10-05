from dataclasses import dataclass
from enum import StrEnum


class InboxCategory(StrEnum):
    INVOICE = "invoice"
    PROMO = "promo"
    MEETING = "meeting"
    TASK = "task"
    TRAINING = "training"
    SPAM = "spam"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Classification:
    category: InboxCategory
    confidence: float
    reason: str


def classify_subject(subject: str, sender: str = "") -> Classification:
    value = f"{subject} {sender}".casefold()
    rules = (
        (("invoice", "bill", "statement", "purchase order"), InboxCategory.INVOICE, 0.96),
        (("sale", "promotion", "promo", "special offer"), InboxCategory.PROMO, 0.9),
        (("meeting", "calendar", "invite"), InboxCategory.MEETING, 0.92),
        (("todo", "to-do", "action required", "task"), InboxCategory.TASK, 0.9),
        (("training", "course", "safety certification"), InboxCategory.TRAINING, 0.9),
        (("casino", "winner", "viagra", "unsubscribe"), InboxCategory.SPAM, 0.98),
    )
    for keywords, category, confidence in rules:
        if any(keyword in value for keyword in keywords):
            return Classification(category, confidence, f"matched keyword rule for {category.value}")
    return Classification(InboxCategory.UNKNOWN, 0.35, "no trusted rule matched")


def classify_message(subject: str, sender: str = "", body: str = "") -> Classification:
    """Classify trusted envelope fields; never execute or follow instructions in message text.

    The body is intentionally excluded from the rules. Email content is untrusted data and
    may contain prompt-injection text such as requests to change the assigned category.
    """
    del body
    return classify_subject(subject, sender)
