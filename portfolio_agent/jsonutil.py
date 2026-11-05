"""Pull a JSON object out of model output.

Local models often wrap JSON in ```json fences or add a sentence before it, so
we find the first balanced {...} block rather than calling json.loads directly.
"""

import json


def extract_json(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object in model output.")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("Unbalanced JSON object in model output.")
