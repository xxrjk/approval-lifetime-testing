"""Mechanical source-level mutants of the approval-relevant persistence code.

Set A was examined during method development and includes the corrected
multi-line conjunct mutations S000-S002. Set B uses statement deletion and was
generated after the complete-obligation checker was frozen.

Operators are applied exhaustively to eligible sites in the listed functions.

  ROR        swap one '!=' <-> '==' occurrence (outside string literals)
  NEG        negate one if-condition
  GUARD_DEL  delete one single-line early-return guard  `if (...) return ...;`
  RET_TRUE   `return false;` / `return X == 1;`  ->  `return true;`
  CONJ_TRUE  replace one conjunct of a multi-line && chain with `true`
  MAP_DEL    delete one entry of a persisted update map
  WHERE_WEAK drop the status guard from an update's WHERE clause (+ its argument)
  SQL_DEL    delete one assignment / predicate inside an SQL string
  TRANS_ADD  widen one lifecycle-transition set by the missing state
  FIELD_DEL  drop one field from the content-binding hash
"""
from __future__ import annotations
import re
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / 'app/lib'
DB_FUNCS = ['static Future<void> updateCardText(', 'static List<String> _persistedEvidenceGroups(',
            'static bool _quotesBelongToIndividualBlocks(', 'static bool _standardAuditContractHolds(',
            'static Future<bool> updateCardAudit({', 'static Future<bool> revokeCardApproval(',
            'static const Map<String, Set<String>> _standardStatusTransitions = {',
            'static Future<bool> updateCardStatus(', 'static Future<int> approvePending(']


def regions(lines, heads):
    out = []
    for head in heads:
        start = next(i for i, l in enumerate(lines) if head in l)
        depth = 0; seen = False; arrow = False; paren = 0
        for j in range(start, len(lines)):
            code = strip_strings(lines[j])
            if not seen and '=>' in code and '{' not in code.split('=>')[0][code.find('('):] if False else (not seen and '=>' in code):
                arrow = True
            paren += code.count('(') - code.count(')')
            if arrow:
                if paren <= 0 and code.rstrip().endswith(';'):
                    out.append((head, start, j)); break
                continue
            depth += code.count('{') - code.count('}')
            seen = seen or '{' in code
            if seen and depth <= 0:
                out.append((head, start, j)); break
    return out


def strip_strings(line):
    return re.sub(r"'[^']*'|\"[^\"]*\"", lambda m: '_' * len(m.group(0)), line)


def match_paren(s, i):
    depth = 0
    for j in range(i, len(s)):
        if s[j] == '(':
            depth += 1
        elif s[j] == ')':
            depth -= 1
            if depth == 0:
                return j
    return None


def db_mutants(text):
    lines = text.split('\n'); muts = []
    for head, a, b in regions(lines, DB_FUNCS):
        fn = ('_standardStatusTransitions' if 'Transitions' in head
              else head.split('(')[0].split()[-1].strip())
        for i in range(a + 1, b + 1):
            line = lines[i]; code = strip_strings(line)
            for m in re.finditer(r'(!=|==)', code):
                new = line[:m.start()] + ('==' if m.group(1) == '!=' else '!=') + line[m.end():]
                muts.append(('ROR', fn, i, {i: new}))
            k = code.find('if (')
            if k >= 0:
                e = match_paren(line, k + 3)
                if e is not None:
                    new = line[:k + 3] + '(!(' + line[k + 4:e] + '))' + line[e + 1:]
                    muts.append(('NEG', fn, i, {i: new}))
                    if re.search(r'\)\s*return [^;]+;\s*$', line):
                        muts.append(('GUARD_DEL', fn, i, {i: '// mutant: guard deleted'}))
            if re.search(r'return (false|\w+ == 1);', line):
                muts.append(('RET_TRUE', fn, i, {i: re.sub(r'return (false|\w+ == 1);', 'return true;', line)}))
            if fn in ('_standardAuditContractHolds', '_quotesBelongToIndividualBlocks') and code.rstrip().endswith('&&'):
                indent = line[:len(line) - len(line.lstrip())]
                muts.append(('CONJ_TRUE', fn, i, {i: indent + 'true &&'}))
            if fn in ('updateCardText', 'updateCardAudit') and re.match(r"\s*'[a-z_]+':", line):
                edits = {}; bal = 0; j = i
                while True:
                    c = strip_strings(lines[j]); bal += sum(c.count(x) for x in '([{') - sum(c.count(x) for x in ')]}')
                    edits[j] = '// mutant: map entry deleted'
                    if bal <= 0 and c.rstrip().endswith(','):
                        break
                    j += 1
                muts.append(('MAP_DEL', fn, i, edits))
            if "where: 'id=? AND status=?'," in line and i + 1 <= b and 'whereArgs: [id,' in lines[i + 1]:
                muts.append(('WHERE_WEAK', fn, i, {i: line.replace("'id=? AND status=?'", "'id=?'"),
                                                   i + 1: re.sub(r'whereArgs: \[id, [^\]]+\]', 'whereArgs: [id]', lines[i + 1])}))
            if fn == 'revokeCardApproval':
                for seg in ['approval_epoch=approval_epoch+1, ', "status='pending',", "audit_status='stale', ",
                            'audit_content_hash=NULL, ', 'audit_approval_epoch=NULL,', "audit_metadata='{}', "]:
                    if seg in line:
                        muts.append(('SQL_DEL', fn, i, {i: line.replace(seg, '')}))
            if fn == 'approvePending':
                for seg in [" AND status='pending'", " AND audit_status='passed'", "WHERE status='pending' AND "]:
                    if seg in line:
                        rep = 'WHERE ' if seg.startswith('WHERE') else ''
                        muts.append(('SQL_DEL', fn, i, {i: line.replace(seg, rep)}))
                if 'if (_standardAuditContractHolds(card))' in line:
                    muts.append(('CONJ_TRUE', fn, i, {i: line.replace('_standardAuditContractHolds(card)', 'true')}))
            if fn == '_standardStatusTransitions':
                m = re.match(r'(\s*CardStatus\.(\w+): \{)([^}]*)(\},)', line)
                if m:
                    have = set(re.findall(r'CardStatus\.(\w+)', m.group(3)))
                    for st in ['pending', 'active', 'suspended']:
                        if st not in have and st != m.group(2):
                            muts.append(('TRANS_ADD', fn, i, {i: f'{m.group(1)}{m.group(3)}, CardStatus.{st}{m.group(4)}'}))
    return muts


def binding_mutants(text):
    lines = text.split('\n'); muts = []
    for i, line in enumerate(lines):
        m = re.match(r"\s*'(\w+)': card\.\w+,", line)
        if m:
            muts.append(('FIELD_DEL', 'cardAuditContentHash', i, {i: f'// mutant: {m.group(1)} dropped'}))
    return muts


def all_mutants():
    db = (LIB/'db.dart').read_text(); bd = (LIB/'audit_binding.dart').read_text()
    out = []
    for op, fn, i, edits in db_mutants(db):
        out.append({'file': 'db.dart', 'op': op, 'function': fn, 'line': i + 1, 'edits': edits})
    for op, fn, i, edits in binding_mutants(bd):
        out.append({'file': 'audit_binding.dart', 'op': op, 'function': fn, 'line': i + 1, 'edits': edits})
    for k, m in enumerate(out):
        m['id'] = f'M{k:03d}'
    return out


def apply(mutant, text):
    lines = text.split('\n')
    for i, new in mutant['edits'].items():
        lines[int(i)] = new
    return '\n'.join(lines)


def supplementary(db):
    """Corrected replacements for two stillborn conjunct mutants and the last conjunct."""
    lines = db.split('\n'); out = []
    i = next(k for k, l in enumerate(lines) if 'return card.auditStatus == AuditStatus.passed &&' in l)
    out.append({'id': 'S000', 'file': 'db.dart', 'op': 'CONJ_TRUE', 'function': '_standardAuditContractHolds',
                'line': i + 1, 'edits': {i: '    return true &&'}})
    j = next(k for k, l in enumerate(lines) if '(probability == null ||' in l)
    out.append({'id': 'S001', 'file': 'db.dart', 'op': 'CONJ_TRUE', 'function': '_standardAuditContractHolds',
                'line': j + 1, 'edits': {j: '        true &&', j + 1: '        // mutant: conjunct removed'}})
    k = next(k for k, l in enumerate(lines) if '_quotesBelongToIndividualBlocks(quotes, _persistedEvidenceGroups(card));' in l)
    out.append({'id': 'S002', 'file': 'db.dart', 'op': 'CONJ_TRUE', 'function': '_standardAuditContractHolds',
                'line': k + 1, 'edits': {k: '        true;'}})
    return out


def _balance(code):
    return sum(code.count(x) for x in '([{') - sum(code.count(x) for x in ')]}')


def statement_deletion_mutants():
    """Generate Set B by deleting each complete statement in the approval functions.

    Statements start at a statement boundary and end with balanced brackets and a
    semicolon. Control headers open a body that is scanned recursively. Every
    eligible statement contributes a mutation.
    """
    db = (LIB/'db.dart').read_text(); out = []
    lines = db.split('\n')
    for head, a, b in regions(lines, DB_FUNCS):
        fn = ('_standardStatusTransitions' if 'Transitions' in head
              else head.split('(')[0].split()[-1].strip())
        i = a + 1; boundary = True
        while i <= b:
            code = strip_strings(lines[i]).strip()
            if not code or code.startswith('//'):
                i += 1; continue
            if boundary and not code.startswith('}') and not code.endswith('{') and not code.startswith(')'):
                bal = 0; paren = 0; j = i; ok = False; header = None
                while j <= b:
                    c = strip_strings(lines[j])
                    bal += _balance(c)
                    paren += c.count('(') - c.count(')') + c.count('[') - c.count(']')
                    if bal < 0:
                        break
                    if paren == 0 and c.rstrip().endswith('{'):
                        header = j; break      # multi-line control header: scan its body instead
                    if bal == 0 and c.rstrip().endswith(';'):
                        ok = True; break
                    j += 1
                if header is not None:
                    i = header + 1; boundary = True; continue
                if ok:
                    out.append({'file': 'db.dart', 'op': 'SDL', 'function': fn, 'line': i + 1,
                                'edits': {k: '// mutant: statement deleted' for k in range(i, j + 1)}})
                    i = j + 1; boundary = True; continue
            boundary = code.endswith((';', '{', '}', '},', '});', '),'))
            i += 1
    for k, m in enumerate(out):
        m['id'] = f'B{k:03d}'
    return out


def set_a():
    base = (LIB/'db.dart').read_text()
    return all_mutants() + supplementary(base)


if __name__ == '__main__':
    from collections import Counter
    ms = all_mutants()
    print(len(ms), Counter(m['op'] for m in ms), Counter(m['function'] for m in ms))
    src = {'db.dart': (LIB/'db.dart').read_text(), 'audit_binding.dart': (LIB/'audit_binding.dart').read_text()}
    for m in ms:
        first = min(m['edits'])
        old = src[m['file']].split('\n')[int(first)].strip()
        print(m['id'], m['op'], m['function'], m['line'], '|', old[:70], '=>', m['edits'][first].strip()[:70])
