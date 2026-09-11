"""Read agent JSON without rewriting prose or guessing missing values."""
import json
import logging


def loads(raw):
    original = raw
    # Only remove redundant object separators at positions identified by the
    # strict parser. It cannot point inside a valid string or an array gap.
    for _ in range(17):
        try:
            result = json.loads(raw)
            if raw != original:
                logging.info('Normalized redundant JSON object commas; original response retained')
            return result
        except json.JSONDecodeError as exc:
            if exc.msg != 'Expecting property name enclosed in double quotes':
                raise
            previous = exc.pos - 1
            while previous >= 0 and raw[previous].isspace():
                previous -= 1
            if previous < 0 or raw[previous] != ',' or raw[exc.pos:exc.pos + 1] not in (',', '}'):
                raise
            raw = raw[:previous] + raw[previous + 1:]
    # Excessive corruption is still rejected, with no recursive repair loop.
    raise ValueError('Too many redundant JSON object separators')
