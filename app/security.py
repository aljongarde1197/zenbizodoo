MAX_SEARCH_LENGTH = 200
ALLOWED_SALES_STATES = {"", "draft", "sent", "sale", "done", "cancel"}
ALLOWED_PICKING_STATES = {"", "draft", "waiting", "confirmed", "assigned", "done", "cancel"}

def clamp_limit(value: int, maximum: int) -> int:
    return max(1, min(value, maximum))

def clean_search(value: str | None) -> str:
    return (value or "").strip()[:MAX_SEARCH_LENGTH]

def positive_id(value: int, label: str) -> int:
    if value <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    return value

def choice(value: str, allowed: set[str], label: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in allowed:
        options = ", ".join(sorted(x for x in allowed if x))
        raise ValueError(f"Invalid {label}. Use: {options}, or leave blank.")
    return normalized
