from dataclasses import dataclass


@dataclass(frozen=True)
class InboxAddress:
    slug: str
    token: str


def parse_inbox_address(recipient: str) -> InboxAddress:
    normalized = recipient.strip().casefold()
    if "@" not in normalized:
        raise ValueError("recipient must include a domain")
    local_part = normalized.split("@", maxsplit=1)[0]
    try:
        slug, token = local_part.rsplit("-", maxsplit=1)
    except ValueError as error:
        raise ValueError("recipient must use slug-token@domain format") from error
    if not slug or not token:
        raise ValueError("recipient must include both slug and token")
    return InboxAddress(slug=slug, token=token)
