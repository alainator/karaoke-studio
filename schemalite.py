"""A small JSON Schema (2020-12) checker for contract/schemas: the studio checks what players send against the
contract's own schemas, and the tests check every answer. Only the keywords those schemas use are implemented; any
other keyword raises, so a schema can't silently go unchecked (the image has no jsonschema package).

    errors(schema, data) -> ["$.plays[0].pid: doesn't match ^[0-9a-f]{8}-...", ...]   ([] = valid)
    load(name) -> the schema contract/schemas/<name> (cached)
"""
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMAS = os.path.join(HERE, "contract", "schemas")
ANNOTATIONS = {"$schema", "$id", "title", "description", "$defs", "default", "examples", "$comment"}
_cache = {}


def load(name):
    if name not in _cache:
        with open(os.path.join(SCHEMAS, name), encoding="utf-8") as f:
            _cache[name] = json.load(f)
    return _cache[name]


def _type_ok(t, x):
    if t == "null":
        return x is None
    if t == "boolean":
        return isinstance(x, bool)
    if t == "integer":
        return isinstance(x, int) and not isinstance(x, bool) or (isinstance(x, float) and x.is_integer())
    if t == "number":
        return isinstance(x, (int, float)) and not isinstance(x, bool)
    if t == "string":
        return isinstance(x, str)
    if t == "array":
        return isinstance(x, list)
    if t == "object":
        return isinstance(x, dict)
    raise ValueError(f"unknown type {t!r}")


def _ref(root, ref):
    if not ref.startswith("#/"):
        raise ValueError(f"only local $ref is supported: {ref}")
    node = root
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _check(s, x, path, root, out):
    if s is True or s == {}:
        return
    if s is False:
        out.append(f"{path}: not allowed")
        return
    unknown = set(s) - ANNOTATIONS - KEYWORDS
    if unknown:
        raise ValueError(f"schema keyword(s) not supported: {sorted(unknown)} at {path}")
    if "$ref" in s:
        _check(_ref(root, s["$ref"]), x, path, root, out)
    if "type" in s:
        ts = s["type"] if isinstance(s["type"], list) else [s["type"]]
        if not any(_type_ok(t, x) for t in ts):
            out.append(f"{path}: should be {' or '.join(ts)}")
            return
    if "enum" in s and not any(x == e and type(x) is type(e) or (x is None and e is None) for e in s["enum"]):
        out.append(f"{path}: should be one of {s['enum']}")
    if "const" in s and not (x == s["const"] and type(x) is type(s["const"])):
        out.append(f"{path}: should be {s['const']!r}")
    if isinstance(x, str):
        if "minLength" in s and len(x) < s["minLength"]:
            out.append(f"{path}: shorter than {s['minLength']}")
        if "maxLength" in s and len(x) > s["maxLength"]:
            out.append(f"{path}: longer than {s['maxLength']}")
        if "pattern" in s and not re.search(s["pattern"], x):
            out.append(f"{path}: doesn't match {s['pattern']}")
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        if "minimum" in s and x < s["minimum"]:
            out.append(f"{path}: below {s['minimum']}")
        if "maximum" in s and x > s["maximum"]:
            out.append(f"{path}: above {s['maximum']}")
        if "exclusiveMinimum" in s and x <= s["exclusiveMinimum"]:
            out.append(f"{path}: should be above {s['exclusiveMinimum']}")
    if isinstance(x, list):
        if "minItems" in s and len(x) < s["minItems"]:
            out.append(f"{path}: fewer than {s['minItems']} items")
        if "maxItems" in s and len(x) > s["maxItems"]:
            out.append(f"{path}: more than {s['maxItems']} items")
        pre = s.get("prefixItems") or []
        for i, (ps, xi) in enumerate(zip(pre, x)):
            _check(ps, xi, f"{path}[{i}]", root, out)
        if "items" in s:
            for i, xi in enumerate(x[len(pre):], start=len(pre)):
                _check(s["items"], xi, f"{path}[{i}]", root, out)
    if isinstance(x, dict):
        for k in s.get("required", []):
            if k not in x:
                out.append(f"{path}: {k} is missing")
        props = s.get("properties", {})
        for k, v in x.items():
            if k in props:
                _check(props[k], v, f"{path}.{k}", root, out)
            elif "additionalProperties" in s:
                _check(s["additionalProperties"], v, f"{path}.{k}", root, out)
        if "maxProperties" in s and len(x) > s["maxProperties"]:
            out.append(f"{path}: more than {s['maxProperties']} fields")
    for sub in s.get("allOf", []):
        _check(sub, x, path, root, out)
    if "oneOf" in s:
        n = sum(1 for sub in s["oneOf"] if not _errs(sub, x, path, root))
        if n != 1:
            out.append(f"{path}: should match exactly one of the oneOf choices (matches {n})")
    if "not" in s and not _errs(s["not"], x, path, root):
        out.append(f"{path}: matches what it must not ({json.dumps(s['not'])[:80]})")
    if "if" in s:
        if not _errs(s["if"], x, path, root):
            if "then" in s:
                _check(s["then"], x, path, root, out)
        elif "else" in s:
            _check(s["else"], x, path, root, out)


def _errs(s, x, path, root):
    out = []
    _check(s, x, path, root, out)
    return out


KEYWORDS = {"$ref", "type", "enum", "const", "minLength", "maxLength", "pattern", "minimum", "maximum",
            "exclusiveMinimum", "minItems", "maxItems", "prefixItems", "items", "required", "properties",
            "additionalProperties", "maxProperties", "allOf", "oneOf", "not", "if", "then", "else"}


def errors(schema, data):
    """Everything wrong with data, as short messages ([] = valid). schema: a dict or a contract schema file name."""
    if isinstance(schema, str):
        schema = load(schema)
    return _errs(schema, data, "$", schema)
