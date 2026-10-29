"""Small HTML builders for every page.

Pages call these instead of repeating f-string markup. Scalars (titles, labels, captions, hints,
summaries) are escaped here; arguments documented as markup (card bodies, table cells, actions)
are inserted as given, so the caller escapes its own values with `e()`.
"""

from __future__ import annotations

from html import escape


def e(value) -> str:
    """Escaped text; None becomes an empty string."""
    return escape(str(value if value is not None else ""))


def _attrs(**items) -> str:
    """` key="value"` for every non-empty attribute (booleans render when true)."""
    out = []
    for key, value in items.items():
        if value is None or value is False:
            continue
        key = key.rstrip("_").replace("_", "-")
        if key == "cls":
            key = "class"
        if value is True:
            out.append(f" {key}")
        else:
            out.append(f' {key}="{e(value)}"')
    return "".join(out)


def skip_link(target: str = "#main", text: str = "Lewati ke konten") -> str:
    return f'<a class="skip-link" href="{e(target)}">{e(text)}</a>'


def page_header(title: str, subtitle: str = "", actions: str = "", *, back: str = "",
                id: str = "") -> str:
    """The one h1 of a page, an optional subtitle and right-aligned actions (markup)."""
    parts = []
    if back:
        parts.append(f'<p class="note">{back}</p>')
    head = [f"<h1{_attrs(id=id)}>{e(title)}</h1>"]
    if subtitle:
        head.append(f'<p class="sub">{e(subtitle)}</p>')
    parts.append(f'<div class="page-head"><div>{"".join(head)}</div>'
                 + (f'<div class="actions">{actions}</div>' if actions else "")
                 + "</div>")
    return "".join(parts)


def card(body: str, title: str = "", *, id: str = "", cls: str = "", level: int = 2) -> str:
    """A content card; `body` is markup."""
    heading = f"<h{level}>{e(title)}</h{level}>" if title else ""
    classes = f"card {cls}".strip()
    return f'<div class="{e(classes)}"{_attrs(id=id)}>{heading}{body}</div>'


def stat(label_text: str, value, hint: str = "", *, tone: str = "", id: str = "") -> str:
    """One number with its label: triage counts, money totals, quotas."""
    return (f'<div class="stat"{_attrs(id=id, data_tone=tone)}>'
            f'<div class="label">{e(label_text)}</div>'
            f'<div class="value">{e(value)}</div>'
            + (f'<div class="hint">{e(hint)}</div>' if hint else "")
            + "</div>")


def stats(items: list[str], *, id: str = "") -> str:
    return f'<div class="stats"{_attrs(id=id)}>{"".join(items)}</div>'


def badge(text, tone: str = "", *, title: str = "", id: str = "") -> str:
    """An inline status chip; `tone` is one of "", ok, warn, bad, low."""
    classes = f"tag {tone}".strip()
    return f'<span class="{e(classes)}"{_attrs(id=id, title=title)}>{e(text)}</span>'


def table(headers: list[str], rows: list[list[str]], *, caption: str = "", num: tuple[int, ...] = (),
          empty: str = "", id: str = "", cls: str = "responsive", head: bool = True,
          row_ids: list[str] | None = None) -> str:
    """A table with a hidden caption and `data-label` on every cell (stacked cards below 720 px).

    `rows` holds markup cells: the caller escapes. `num` lists the column indexes that hold money
    or counts and should be right-aligned. `row_ids` puts a stable DOM anchor on each row."""
    caption_html = f"<caption{' class=visually-hidden' if not caption else ''}>{e(caption)}</caption>" if caption else ""
    if not rows:
        if empty:
            return empty_state(empty)
        return ""
    head_html = ""
    if head:
        cells = "".join(
            f'<th scope="col"{_attrs(cls="num" if i in num else None)}>{e(text)}</th>'
            for i, text in enumerate(headers))
        head_html = f"<thead><tr>{cells}</tr></thead>"
    body = []
    for index, row in enumerate(rows):
        cells = "".join(
            f'<td{_attrs(data_label=headers[i] if i < len(headers) else "", cls="num" if i in num else None)}>'
            f'{cell}</td>' for i, cell in enumerate(row))
        rid = (row_ids or [])[index] if row_ids and index < len(row_ids) else ""
        body.append(f"<tr id='{e(rid)}'>{cells}</tr>" if rid else f"<tr>{cells}</tr>")
    classes = f"table {cls}".strip()
    id_attr = f' id="{e(id)}"' if id else ""
    return (f'<div class="table-wrap"><table{id_attr}{_attrs(cls=classes)}>{caption_html}{head_html}'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def empty_state(text: str, action: str = "", *, id: str = "") -> str:
    """An empty list that names the next action; `action` is markup (a button or link)."""
    return (f'<div class="empty"{_attrs(id=id)}><p>{e(text)}</p>'
            + (action or "") + "</div>")


def field(label_text: str, control: str, *, hint: str = "", id: str = "", for_: str = "",
          required: bool = False, cls: str = "") -> str:
    """A labelled control; `control` is markup, `label_text` and `hint` are escaped."""
    label_for = f' for="{e(for_)}"' if for_ else ""
    req = ' <span class="req" aria-hidden="true">*</span>' if required else ""
    hint_html = f'<div class="hint">{e(hint)}</div>' if hint else ""
    classes = f"field {cls}".strip()
    return (f'<div class="{e(classes)}"{_attrs(id=id)}>'
            f'<label class="label"{label_for}>{e(label_text)}{req}</label>'
            f"{control}{hint_html}</div>")


def button(text: str, *, variant: str = "secondary", type: str = "submit", name: str = "",
           value: str = "", id: str = "", disabled: bool = False, title: str = "",
           block: bool = False) -> str:
    classes = {"primary": "btn primary", "secondary": "btn", "quiet-danger": "btn quiet-danger",
               "block": "btn primary block"}.get(variant, f"btn {variant}")
    if block and "block" not in classes:
        classes += " block"
    return (f'<button{_attrs(type=type, name=name, value=value, id=id, class_=classes, disabled=disabled, title=title)}>'
            f"{e(text)}</button>")


def alert(text: str, *, tone: str = "info", role: str = "status", id: str = "") -> str:
    """A flash (`status`) or an error (`alert`); it takes focus so a redirect announces it."""
    return (f'<div class="alert"{_attrs(id=id, data_tone=tone, role=role, tabindex="-1")}>'
            f"{e(text)}</div>")


def stepper(steps: list[str], current: int = 0, *, id: str = "") -> str:
    """A progress list; the current step carries aria-current, earlier steps are marked done."""
    items = []
    for i, text in enumerate(steps):
        current_attr = ' aria-current="step"' if i == current else ""
        done = ' data-done="true"' if i < current else ""
        items.append(f"<li{current_attr}{done}>{e(text)}</li>")
    return f'<ol class="stepper"{_attrs(id=id)}>{"".join(items)}</ol>'


def details(summary: str, body: str, *, id: str = "", open: bool = False) -> str:
    """Progressive disclosure; `summary` is escaped, `body` is markup."""
    return f'<details{_attrs(id=id, open=open)}><summary>{e(summary)}</summary>{body}</details>'


def chips(values: list[str], *, tone: str = "") -> str:
    """A row of badges; each value is escaped."""
    return f'<div class="chips">{"".join(badge(v, tone) for v in values)}</div>'
