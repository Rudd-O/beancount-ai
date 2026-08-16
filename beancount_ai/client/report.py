import html
import urllib.parse


def unquote(s: str) -> str:
    """Remove surrounding double-quotes and unescape `\\\\` → `\\`, `\\"` → `"`."""
    if not s.startswith('"') or not s.endswith('"'):
        return s
    inner = s[1:-1]
    return (
        inner.replace("\\\\", "\x00")  # placeholder for literal backslashes
        .replace('\\"', '"')  # unescape quotes first
        .replace("\x00", "\\")  # restore backslashes
    )


def append_to_report(reporttext: str, diff: list[str]) -> str:
    if not reporttext:
        reporttext = """<html>
                <head>
                <style type=\"text/css\">
                .rem { background-color: rgb(255, 235, 233); }
                .add { background-color: #dafbe1; }
                body { font-family: monospace; }
                </style>
                </head>
                <body>
                </body>"""
    endpos = reporttext.find("</body>")
    before, after = reporttext[:endpos], reporttext[endpos:]
    procdifflines: list[str] = []
    for line in diff:
        line = line.rstrip("\n")
        if line.startswith("--- ") and line != "--":
            line = '<div class="rem">' + html.escape(line) + "</div>"
        elif line.startswith("+++ "):
            line = '<div class="add">' + html.escape(line) + "</div>"
        elif line.startswith("-"):
            line = '<div class="rem">' + html.escape(line) + "</div>"
        elif line.startswith("+"):
            potential_doc = line[1:]
            if potential_doc.strip().startswith("document"):
                p, sep, q = line.partition(": ")
                url = unquote(q)
                url_for_link = "file://" + urllib.parse.quote(url)
                onclick = "window.open(this.href, 'newwindow', 'width=800,height=1200')"
                q = f'<a target="_blank" disabled_onclick="{onclick}" href="{url_for_link}">{q}</a>'
                line = p + sep + q
            line = '<div class="add">' + line + "</div>"
        else:
            line = "<div>" + html.escape(line) + "</div>"
        procdifflines.append(line)
    text = "\n<hr/><pre>" + "".join(procdifflines) + "</pre>\n"
    reporttext = before + text + after
    return reporttext
