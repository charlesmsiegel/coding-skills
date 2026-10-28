"""Bounded lexical binding and enum compatibility evidence for scaffolding.

No TypeScript compiler is required. Unresolved enum expressions remain candidates.
"""
import re
from collections import defaultdict


def _string_name(raw):
    """Decode JS member escapes; unsupported escapes are unknown, not distinct."""
    if raw[:1] not in {'"', "'"}:
        return raw
    text = raw[1:-1]
    escapes = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f', 'v': '\v',
               '0': '\0', '\\': '\\', '"': '"', "'": "'"}
    pattern = r"\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)"
    unknown = False

    def decode(match):
        nonlocal unknown
        code = match.group(1)
        if code.startswith(('u', 'x')):
            try:
                return chr(int(code[1:].strip('{}'), 16))
            except ValueError:
                unknown = True
                return ''
        if code in escapes:
            return escapes[code]
        unknown = True
        return ''

    result = re.sub(pattern, decode, text)
    return None if unknown or '\\' in result else result


def _enum_status(file, declarations):
    """Return compatible, conflict, or unknown for a set of enum bodies."""
    if len({d.is_const for d in declarations}) > 1:
        return 'conflict'
    names, implicit_heads = set(), 0
    unknown = False
    for decl in declarations:
        opening = next((i for i in range(decl.start, decl.end) if file.value(i) == '{'), -1)
        if opening < 0:
            unknown = True
            continue
        cursor, first, previous = opening + 1, True, 'numeric'
        while cursor < decl.end:
            if file.value(cursor) == ',':
                cursor += 1
                continue
            token = file.tokens[cursor]
            name = _string_name(token.value) if token.kind in {'name', 'str'} else None
            # The lexer deliberately leaves identifier Unicode escapes as raw
            # tokens. Read their complete source span before comparing bindings.
            name_start = token.start
            if name_start > 0 and file.text[name_start - 1] == '\\':
                name_start -= 1
            raw_name = re.match(r'(?:[A-Za-z_$\w]|\\u(?:[0-9a-fA-F]{4}|\{[0-9a-fA-F]+\}))+',
                                file.text[name_start:]) if token.kind == 'name' else None
            if raw_name and '\\' in raw_name.group(0):
                raw = raw_name.group(0)
                name = _string_name('"' + raw + '"')
                end = name_start + len(raw)
                while cursor + 1 < decl.end and file.tokens[cursor + 1].start < end:
                    cursor += 1
            if name is None:
                unknown = True
            elif name in names:
                return 'conflict'
            else:
                names.add(name)
            cursor += 1
            if file.value(cursor) != '=':
                if first:
                    implicit_heads += 1
                elif previous == 'string':
                    return 'conflict'
                elif previous == 'computed':
                    return 'conflict'
                elif previous == 'unknown':
                    # It may be a numeric constant reference; no proof either way.
                    unknown = True
                previous = 'numeric'
            else:
                cursor += 1
                start = cursor
                while cursor < decl.end and file.value(cursor) != ',':
                    if file.value(cursor) in {'(', '[', '{'}:
                        cursor = file.closer(cursor) + 1
                    else:
                        cursor += 1
                expression = file.slice(start, cursor).strip()
                if re.fullmatch(r'[+-]?(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*(?:\.\d*)?)', expression):
                    previous = 'numeric'
                elif cursor == start + 1 and file.tokens[start].kind == 'str':
                    previous = 'string'
                else:
                    previous = 'computed' if re.search(r'[A-Za-z_$][\w$]*\s*\(', expression) else 'unknown'
                    unknown = True
            first = False
    if implicit_heads > 1:
        return 'conflict'
    return 'unknown' if unknown else 'compatible'


def _interface_status(file, declarations):
    """Prove simple property merges; complex contracts need compiler review."""
    properties, unknown = {}, False
    for decl in declarations:
        opening = next((i for i in range(decl.start, decl.end) if file.value(i) == '{'), -1)
        if opening < 0:
            unknown = True
            continue
        header = file.slice(decl.start, opening)
        if '<' in header or decl.extends:
            unknown = True
        body = file.slice(opening + 1, decl.end).strip()
        for member in re.split(r'[;,]', body):
            if not member.strip():
                continue
            match = re.fullmatch(r'\s*(readonly\s+)?([A-Za-z_$][\w$]*)(\?)?\s*:\s*(string|number|boolean|unknown|never|any)\s*', member)
            if match is None:
                unknown = True
                continue
            readonly, name, optional, kind = match.groups()
            contract = (bool(readonly), bool(optional), kind)
            if name in properties and properties[name] != contract:
                return 'conflict'
            properties[name] = contract
    return 'unknown' if unknown else 'compatible'


def _scopes(file):
    """Each brace has its own identity; exported namespace bodies also have a path.

    Namespace paths retain their enclosing lexical identity so two function-local
    namespaces never accidentally share a binding.
    """
    namespaces = {}
    for i, token in enumerate(file.tokens):
        if not token.is_name('namespace', 'module'):
            continue
        cursor, names = i + 1, []
        while cursor < len(file.tokens) and (file.tokens[cursor].kind in {'name', 'str'}
                                            or file.value(cursor) == '.'):
            if file.value(cursor) != '.':
                names.append(file.value(cursor))
            cursor += 1
        if names and file.value(cursor) == '{':
            modifiers = []
            before = i - 1
            while before >= 0 and file.value(before) in {'declare', 'export', 'default'}:
                modifiers.append(file.value(before))
                before -= 1
            namespaces[cursor] = (tuple(names), 'declare' in modifiers, 'export' in modifiers)
    stack, scopes, paths, ambient = [], {}, {}, {}
    for i, token in enumerate(file.tokens):
        scopes[i] = stack[-1] if stack else -1
        if token.value == '{':
            parent = stack[-1] if stack else -1
            if i in namespaces:
                names, declared, exported = namespaces[i]
                inherited_ambient = ambient.get(parent, False) or file.path.name.endswith('.d.ts')
                # A private nested namespace belongs to this particular body.
                base = paths.get(parent, ('lexical', parent)) if exported or inherited_ambient else ('lexical', parent)
                paths[i] = base + names
                ambient[i] = declared or inherited_ambient
            stack.append(i)
        elif token.value == '}' and stack:
            stack.pop()
    return scopes, paths, ambient


def check_duplicate_bindings(file, report):
    scopes, paths, ambient = _scopes(file)
    records = []
    for func in file.functions:
        if func.kind == 'function' and func.name != '<anonymous>' and func.has_body:
            records.append((func, 'function', {'value'}))
    for klass in file.classes:
        if klass.name != '<anonymous>':
            records.append((klass, 'class', {'type', 'value'}))
    for decl in file.types:
        records.append((decl, decl.kind, {'type', 'value'} if decl.kind == 'enum' else {'type'}))
    groups = defaultdict(list)
    for record in records:
        decl, kind, _ = record
        scope = scopes.get(decl.start, -1)
        groups[('local', scope, decl.name)].append(record)
        if scope in paths and (decl.is_exported or ambient[scope]):
            groups[('namespace', paths[scope], decl.name)].append(record)
    # A local binding and its exported namespace binding describe the same
    # declaration, so combine them before diagnosing (not output suppression).
    parents = {d.start: d.start for d, _, _ in records}

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for group in groups.values():
        for record in group[1:]:
            parents[root(record[0].start)] = root(group[0][0].start)
    bindings = defaultdict(list)
    for record in records:
        bindings[root(record[0].start)].append(record)
    for group in bindings.values():
        group = sorted(group, key=lambda r: r[0].start)
        conflicts, uncertain = set(), set()
        for i, (left, left_kind, left_space) in enumerate(group):
            for right, right_kind, right_space in group[i + 1:]:
                if not left_space & right_space:
                    continue
                if left_kind == right_kind == 'interface':
                    continue
                if left_kind == right_kind == 'enum':
                    continue
                pair = (left.start, right.start)
                if {left_kind, right_kind} == {'interface', 'class'}:
                    uncertain.add(pair)
                else:
                    conflicts.add(pair)
        enums = [d for d, kind, _ in group if kind == 'enum']
        status = _enum_status(file, enums) if enums else 'compatible'
        interfaces = [d for d, kind, _ in group if kind == 'interface']
        if len(interfaces) > 1:
            interface_status = _interface_status(file, interfaces)
            if interface_status == 'conflict' or status == 'compatible':
                status = interface_status
        if not conflicts and not uncertain and (status == 'compatible' or len(group) == 1 and status == 'unknown'):
            continue
        lines = sorted({d.line for d, _, _ in group})
        name = group[0][0].name
        description = f'`{name}` has conflicting declarations in one lexical binding (lines {", ".join(map(str, lines))})'
        if conflicts or status == 'conflict':
            report.add(lines[-1], 'duplicate_definition', description,
                       'Resolve the conflicting declarations or enum members in this binding.',
                       'high', related=lines[:-1])
        else:
            report.candidate(lines[-1], 'duplicate_definition',
                             f'`{name}` has declarations whose merge compatibility is unproven',
                             ['TypeScript may permit a declaration merge; computed initializers or member contracts '
                              'require compiler-level checking.'], related=lines[:-1])
