import pytest
from qnapnewshub.document_blocks import blocks, compare, MAX_BYTES, MAX_BLOCKS


def test_unchanged_and_shift():
    a = '# A\n\nfirst\n\nsecond\n\nthird\n'
    b = '# A\n\ninserted\n\nfirst\n\nsecond\n\nthird\n'
    d = compare(a, b)
    assert not compare(a, a).requires_full_extraction
    assert d.requires_full_extraction and d.added_or_ambiguous == (1,)
    assert not d.removed_or_ambiguous
    assert [(m.old, m.new, m.context_changed) for m in d.matches] == [(0,0,True),(1,2,True),(2,3,False),(3,4,False)]


def test_duplicate_is_ambiguous():
    d = compare('same\n\nsame\n', 'same\n')
    assert not d.matches and d.removed_or_ambiguous == (0,1) and d.added_or_ambiguous == (0,)


def test_heading_context():
    d = compare('# Old\n\nbody\n\nnext\n', '# New\n\nbody\n\nnext\n')
    assert len(d.matches) == 2 and all(m.context_changed for m in d.matches)


def test_table_not_split_into_rows():
    a = '| model | ROCm |\n| --- | --- |\n| A | 10 |\n'
    b = a.replace('ROCm','Vulkan')
    assert len(blocks(a)) == 1 and not compare(a,b).matches


def test_fences_and_raw_offsets():
    s = '# H\r\n\r\n```md\r\n\r\n# not a heading\r\n```\r\n\r\n日本語😀\r\n'
    bs = blocks(s)
    assert [b.kind for b in bs] == ['heading','fence','text']
    assert s[bs[1].start:bs[1].end] == '```md\r\n\r\n# not a heading\r\n```\r\n'
    assert s[bs[2].start:bs[2].end] == '日本語😀\r\n'


def test_unclosed_and_longer_close():
    assert blocks('~~~\n\nx\n')[0].kind == 'unclosed_fence'
    assert blocks('```\nx\n````\n')[0].kind == 'fence'


def test_deleted_and_moved():
    d = compare('A\n\nB\n\nC\n', 'C\n\nA\n')
    assert d.removed_or_ambiguous == (1,) and not d.added_or_ambiguous
    assert [(m.old,m.new) for m in d.matches] == [(2,0),(0,1)]
    assert all(m.context_changed for m in d.matches)


def test_blank_change_and_empty():
    assert compare('', '\n').requires_full_extraction
    assert not blocks(' \n\t\n')
    assert compare('A\n\nB\n', 'A\n\n\nB\n').requires_full_extraction


def test_limits():
    with pytest.raises(ValueError): blocks('あ' * MAX_BYTES)
    with pytest.raises(ValueError): blocks('x\n\n' * (MAX_BLOCKS+1))
