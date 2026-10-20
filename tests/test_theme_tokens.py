"""app.css defines its colour tokens three times on purpose — light, OS-dark
and the toggle's dark — because CSS can't OR a media query with an attribute
selector (see the comment above the media block). Forgetting one of the three
is the easy mistake: the page then looks right in one mode and quietly wrong
in another, with nothing to notice it. So every --color-* token must exist in
all three."""
import os
import re

CSS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "odoo_edu_manager", "static", "app.css")


def _block(css, header):
    """The body of the `{...}` that follows `header`."""
    depth = 0
    start = css.index("{", css.index(header))
    for i in range(start, len(css)):
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        if depth == 0:
            return css[start + 1:i]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _color_tokens(block):
    return set(re.findall(r"^\s*(--color-[\w-]+)\s*:", block, re.M))


def _themes():
    css = open(CSS_PATH).read()
    return {
        "light": _color_tokens(_block(css, ":root {")),
        "os-dark": _color_tokens(_block(css, ':root:not([data-theme="light"]) {')),
        "toggle-dark": _color_tokens(_block(css, ':root[data-theme="dark"] {')),
    }


def test_every_colour_token_is_defined_in_all_three_theme_blocks():
    themes = _themes()
    everything = set().union(*themes.values())
    missing = {name: sorted(everything - tokens) for name, tokens in themes.items() if everything - tokens}
    assert not missing, f"colour tokens missing from a theme block: {missing}"
