"""Light and dark palettes by role, shared by the dashboard (ui) and the diagram (graph).
60/30/10: neutral canvas and cards; teal, mint and slate blue as the structure; violet and pink only to draw the eye.
Status colours (mint, yellow, orange, red) are kept for data states and never used for decoration. Every colour used as
text reaches at least 4.5:1 contrast on its theme's panel; body text (WHITE, DIM) at least 7:1."""
from devbot import bus

PALETTES = {
    "light": {
        "BG": "#F1F3F5",      # canvas: cool light grey
        "PANEL": "#FFFFFF",   # cards and widgets: pure white
        "RAISED": "#F8F9FA",  # hover / raised surfaces
        "FAINT": "#CED4DA",   # borders and dividers
        "WHITE": "#212529",   # primary text: dark charcoal, never pure black
        "DIM": "#495057",     # secondary text and captions (8.2:1 on white: body text stays above 7:1)
        "INK": "#FFFFFF",     # text on solid chips and buttons
        "TEAL": "#0B7A70",    # primary: headings, navigation, main data lines (5:1 on white)
        "BLUE": "#1E3A5F",    # brand and primary buttons: deep slate navy
        "MINT": "#087F5B",    # success, online, positive
        "VIOLET": "#7C5CDB",  # secondary metrics, charts, active states
        "PINK": "#C2255C",    # highlight accent, used sparingly
        "YELLOW": "#9A6700",  # pending / neutral
        "AMBER": "#C2410C",   # warnings (orange)
        "CORAL": "#C92A2A",   # critical errors (red)
    },
    "dark": {
        "BG": "#1B2329",      # canvas: deep slate grey
        "PANEL": "#252E35",   # cards: layered dark grey
        "RAISED": "#2F3A42",  # hover / raised surfaces
        "FAINT": "#46525C",   # borders and dividers
        "WHITE": "#FFFFFF",   # primary text
        "DIM": "#B8C1CA",     # secondary text: muted cool grey (7.6:1 on the panel)
        "INK": "#13201F",     # text on solid chips and buttons
        "TEAL": "#2DD4BF",    # primary accent
        "BLUE": "#7AA7E8",    # brand and primary buttons
        "MINT": "#A7F3D0",    # success
        "VIOLET": "#C4B5FD",  # secondary accent: pastel violet
        "PINK": "#FF8AD8",    # highlight accent: neon pastel pink
        "YELLOW": "#FFD43B",  # pending / neutral
        "AMBER": "#FFA94D",   # warnings (orange)
        "CORAL": "#FF6B6B",   # critical errors (red)
    },
}
FILE = bus.ROOT / "state" / "theme.txt"


def saved():
    try:
        name = FILE.read_text().strip()
    except FileNotFoundError:
        name = ""
    return name if name in PALETTES else "light"


def apply(name, *modules):
    """Point each module's colour globals at the palette, then let it rebuild anything derived from them."""
    for m in modules:
        for k, v in PALETTES[name].items():
            setattr(m, k, v)
        m.THEME = name
        if hasattr(m, "derive"):
            m.derive()
