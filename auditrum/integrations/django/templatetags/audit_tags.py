from django import template

register = template.Library()


@register.simple_tag
def render_diff(diff: dict) -> str:
    if not diff:
        return "-"
    lines = []
    for field, values in diff.items():
        old, new = values
        lines.append(f"{field}: {old} → {new}")
    return "\n".join(lines)


@register.filter
def changed_fields(diff):
    """Drop diff entries whose ``old`` equals ``new`` (e.g. null→null from the
    synthetic INSERT diff). UPDATE diffs already exclude unchanged fields."""
    if not diff:
        return {}
    return {
        field: change
        for field, change in diff.items()
        if not isinstance(change, dict) or change.get("old") != change.get("new")
    }
