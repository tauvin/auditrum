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
