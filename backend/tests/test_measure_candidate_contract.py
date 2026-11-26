from qnapnewshub.measure_candidate_contract import candidates

def test_explicit_batch_and_thread():
    a = "| model | size | params | backend | ngl | n_batch | test | t/s |\n|---|---|---|---|---|---|---|---|\n| llama | 3 GiB | 7 B | CUDA | -1 | 512 | pp 1024 | 2254.45 ± 15.59 |"
    b = "| model | size | params | backend | threads | test | t/s |\n|---|---|---|---|---|---|---|\n| llama | 3 GiB | 7 B | CPU | 8 | tg 16 | 16.71 ± 0.66 |"
    x,y = candidates(a)[0],candidates(b)[0]
    assert x['conditions'] == {'backend':'cuda','offload':'-1','batch':'512','prompt_tokens':'1024'}
    assert y['conditions'] == {'backend':'cpu','cpu_threads':'8','gen_tokens':'16'}
    assert x['stddev'] == '15.59' and x['status']=='candidate_only'
    assert x['evidence']['cell_text']=='2254.45 ± 15.59'

def test_alias_requires_unique_note_and_no_collision():
    table = "| Model | ROCm tg128 |\n|---|---|\n| Qwen2.5-7B¹ | 52.1 t/s |\n"
    note = "¹ 7B measured earlier."
    assert not candidates(table)[0]['subject_aliases']
    r = candidates(table+'\n'+note)[0]
    assert r['subject']=='Qwen2.5-7B¹'
    assert r['subject_aliases']==[{'text':'Qwen2.5-7B','note_line':5}]
    assert not candidates(table+'\n'+note+'\n'+note)[0]['subject_aliases']
    assert not candidates(table+'| Qwen2.5-7B | 50 t/s |\n\n'+note)[0]['subject_aliases']

def test_depth_not_context_and_unsupported_units():
    raw = "| model | size | params | backend | ngl | test | t/s |\n|---|---|---|---|---|---|---|\n| llama | 3 GiB | 7 B | CUDA | -1 | pp512 @ d512 | 42 ± 1 |"
    r = candidates(raw)[0]
    assert r['conditions_raw']['n_depth']==512 and 'ctx' not in r['conditions']
    assert not candidates("| Step | ROCm pp512 / tg128 |\n|---|---|\n| A | 12 / 3 |")
