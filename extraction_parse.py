"""
Turns the `claude -p` extraction output into a list of items - without ever
quietly turning a failure into "0 quotes".

Before this module, any output that wasn't one perfectly valid JSON array was
logged as a warning and treated as an empty result, and the episode was then
marked 'extracted'. That silently threw away real data twice over:
  - slightly malformed output (e.g. one bad object among 40 good ones)
    discarded ALL of them;
  - "Failed to authenticate: OAuth session expired" was counted as 0 quotes,
    so a login outage marked dozens of episodes done with nothing extracted.

Now:
  1. valid JSON array -> used as is;
  2. malformed -> every complete object is salvaged, and any bad one is skipped;
  3. nothing usable (auth failure, no array at all, only garbage) -> raises, so
     the worker records the episode as an error that can be retried (the
     transcript is kept, so a retry only repeats extraction).
A genuinely empty "[]" is still a valid empty result.

Identical copy lives in ff-dashboard, stock-dashboard and nhl-dashboard.
"""
import json

_DECODER = json.JSONDecoder()


def _strip_fence(output: str) -> str:
    out = output.strip()
    if out.startswith("```"):
        out = out.strip("`")
        if out.startswith("json"):
            out = out[4:]
    return out.strip()


def parse_extraction_output(output: str) -> list:
    out = _strip_fence(output or "")
    if not out:
        raise RuntimeError("claude returned no output")

    start = out.find("[")
    if start == -1:
        if "authenticate" in out.lower() or "oauth" in out.lower() or "login" in out.lower():
            raise RuntimeError(f"claude CLI authentication problem: {out[:200]}")
        raise RuntimeError(f"claude returned no JSON array: {out[:200]}")

    end = out.rfind("]")
    if end > start:
        try:
            parsed = json.loads(out[start:end + 1])
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # Salvage: walk the array object by object, skipping any that don't parse.
    items, skipped, i = [], 0, start + 1
    while i < len(out):
        while i < len(out) and out[i] in " \n\r\t,":
            i += 1
        if i >= len(out) or out[i] != "{":
            break
        try:
            obj, j = _DECODER.raw_decode(out, i)
            if isinstance(obj, dict):
                items.append(obj)
            i = j
        except json.JSONDecodeError:
            skipped += 1
            nxt = out.find("\n{", i + 1)
            if nxt == -1:
                break
            i = nxt + 1

    if not items:
        raise RuntimeError(f"could not parse extraction output: {out[:200]}")
    print(f"WARNING: extraction output was malformed; salvaged {len(items)} item(s), skipped {skipped} bad one(s).")
    return items
