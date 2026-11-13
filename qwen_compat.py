# qwen_compat.py
"""Strict Qwen3-Coder tool-call normalization and JSON-schema subset validation.

This module never executes a tool. Native tool_calls take precedence. A recovered
call must name a tool offered in the same request and passes its argument schema.
"""
import json
import math
import re


class CompatibilityError(ValueError):
    pass


_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_FUNCTION = re.compile(r"<function=(" + _NAME + r")>")
_PARAMETER = re.compile(r"<parameter=(" + _NAME + r")>(.*?)</parameter>", re.S)
_MARKUP = re.compile(r"</?(?:tool_call|function|parameter)(?:[=>\s])")


def unframe(value):
    """Remove one paired framing newline, never arbitrary string whitespace."""
    if value.startswith('\r\n') and value.endswith('\r\n') and len(value) >= 4:
        return value[2:-2]
    if value.startswith('\n') and value.endswith('\n') and len(value) >= 2:
        return value[1:-1]
    return value


def validate(value, schema, path='arguments', _depth=0):
    """Validate the schema features used by the application's native tools."""
    if _depth > 64:
        raise CompatibilityError('Argument nesting exceeds limit at ' + path)
    if not isinstance(schema, dict):
        raise CompatibilityError('Invalid argument schema at ' + path)
    kind = schema.get('type')
    checks = {
        'string': lambda: isinstance(value, str),
        'object': lambda: isinstance(value, dict),
        'array': lambda: isinstance(value, list),
        'boolean': lambda: type(value) is bool,
        'integer': lambda: type(value) is int,
        'number': lambda: type(value) in (int, float) and math.isfinite(value),
        'null': lambda: value is None,
    }
    if kind in checks and not checks[kind]():
        raise CompatibilityError('Invalid type at ' + path + '; expected ' + kind)
    if 'enum' in schema and value not in schema['enum']:
        raise CompatibilityError('Invalid enum at ' + path)
    if type(value) in (int, float):
        if not math.isfinite(value):
            raise CompatibilityError('Non-finite number at ' + path)
        if 'minimum' in schema and value < schema['minimum']:
            raise CompatibilityError('Value below minimum at ' + path)
        if 'maximum' in schema and value > schema['maximum']:
            raise CompatibilityError('Value above maximum at ' + path)
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0):
            raise CompatibilityError('String too short at ' + path)
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            raise CompatibilityError('String too long at ' + path)
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if not set(schema.get('required', [])) <= value.keys():
            raise CompatibilityError('Missing required argument at ' + path)
        extra = set(value) - set(properties)
        additional = schema.get('additionalProperties', True)
        if additional is False and extra:
            raise CompatibilityError('Unknown argument at ' + path + ': ' + ', '.join(sorted(extra)))
        for key, child in value.items():
            if key in properties:
                validate(child, properties[key], path + '.' + key, _depth + 1)
            elif isinstance(additional, dict):
                validate(child, additional, path + '.' + key, _depth + 1)
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            raise CompatibilityError('Array too short at ' + path)
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            raise CompatibilityError('Array too long at ' + path)
        for index, child in enumerate(value):
            validate(child, schema.get('items', {}), path + '[' + str(index) + ']', _depth + 1)
    return value


def normalize(message, model, tools):
    """Return (message, normalization_metadata_or_None), without executing calls."""
    if not isinstance(message, dict):
        raise CompatibilityError('Model message must be an object')
    if message.get('tool_calls'):
        return message, None
    raw = message.get('content', '')
    if not isinstance(raw, str) or not _MARKUP.search(raw):
        return message, None
    family = str(model).split(':')[0].split('/')[-1]
    if not family.startswith('qwen3-coder'):
        return message, None
    if len(raw) > 100000:
        raise CompatibilityError('Compatibility input exceeds limit')
    if '```' in raw:
        raise CompatibilityError('Quoted/fenced tool markup is not executable')
    match = _FUNCTION.search(raw)
    if not match:
        raise CompatibilityError('Missing complete function opener')
    prefix = raw[:match.start()]
    has_wrapper = prefix.rstrip().endswith('<tool_call>')
    if has_wrapper:
        prefix = prefix.rstrip()[:-len('<tool_call>')]
    if _MARKUP.search(prefix):
        raise CompatibilityError('Ambiguous prefix')
    tail = raw[match.start():]
    end = tail.find('</function>')
    if end < 0:
        raise CompatibilityError('Incomplete function block')
    body = tail[len(match.group(0)):end]
    suffix = tail[end + len('</function>'):].strip()
    if suffix != '</tool_call>':
        raise CompatibilityError('Expected one closing tool_call and no trailing data')
    allowed = {
        tool['function']['name']: tool['function']
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get('function'), dict)
    }
    name = match.group(1)
    if name not in allowed:
        raise CompatibilityError('Tool was not offered: ' + name)
    schema = allowed[name].get('parameters', {})
    properties = schema.get('properties', {})
    arguments = {}
    position = 0
    for parameter in _PARAMETER.finditer(body):
        if body[position:parameter.start()].strip():
            raise CompatibilityError('Unrecognized parameter text')
        key, value = parameter.groups()
        if key in arguments or key not in properties:
            raise CompatibilityError('Duplicate/unknown parameter: ' + key)
        if _MARKUP.search(value):
            raise CompatibilityError('Nested/reserved markup in parameter')
        value = unframe(value)
        if properties[key].get('type') != 'string':
            try:
                value = json.loads(value)
            except (ValueError, TypeError) as error:
                raise CompatibilityError('Non-JSON argument: ' + key) from error
        arguments[key] = value
        position = parameter.end()
    if body[position:].strip():
        raise CompatibilityError('Unparsed parameter content')
    validate(arguments, schema)
    call = {'type': 'function', 'function': {'name': name, 'arguments': arguments}}
    normalized = {**message, 'content': prefix.strip(), 'tool_calls': [call]}
    return normalized, {
        'adapter': 'qwen3-coder-function-v1',
        'missing_opening_wrapper': not has_wrapper,
        'raw_content': raw,
        'normalized_call': call,
    }
