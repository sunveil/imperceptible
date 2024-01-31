from bidi.algorithm import get_display
from textblob import TextBlob

# Unicode Bidi override characters
PDF = chr(0x202C)
LRE = chr(0x202A)
RLE = chr(0x202B)
LRO = chr(0x202D)
RLO = chr(0x202E)
PDI = chr(0x2069)
LRI = chr(0x2066)
RLI = chr(0x2067)
# Backspace character
BKSP = chr(0x8)


def bidi(text: str) -> str:
    """Applies Bidi algorithm."""
    text = get_display(text)
    for code in [PDF, LRE, RLE, LRO, RLO, PDI, LRI, RLI]:
        text = text.replace(code, '')
    return text


def defence(text: str) -> str:
    """Applies backspace control characters."""
    while BKSP in text:
        i = text.index(BKSP)
        text = text[:i - 1] + text[i + 1:]
    corrected_text = str(TextBlob(text).correct())
    if corrected_text:
        text = corrected_text
    return bidi(text)
