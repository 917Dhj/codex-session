"""dkitchen: a multi-agent workflow kit. The `ding` CLI lives in `dkitchen.ding`."""


def truncate(text: str, limit: int) -> str:
    return text[:limit] + "…" if len(text) > limit else text
