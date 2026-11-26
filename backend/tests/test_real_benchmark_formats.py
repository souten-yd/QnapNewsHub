"""Published examples and altered negative cases; no network."""
import hashlib
from tools.eval_measure.eval_real_benchmarks import run
from tools.eval_measure.real_benchmark_cases import MD, AMD, VLLM, HOLD
from qnapnewshub.measure_benchmark_formats import benchmark_markdown_records,vllm_log_records


def test_public_numeric_facts():
    totals=run(repeats=1)["totals"]
    assert totals=={"gold":46,"before_tp":4,"after_tp":46,"before_fp":0,"after_fp":0}


def test_native_sd_depth_evidence():
    r=benchmark_markdown_records(MD)
    assert [x['stddev'] for x in r]==['23.45','0.59','18.88','0.60','93.24','0.59']
    assert [x['conditions_raw'].get('n_depth') for x in r]==[None,None,512,512,None,None]
    assert all(x['evidence']['source_sha256']==hashlib.sha256(MD.encode()).hexdigest() for x in r)


def test_reject_ambiguous_and_ungrounded():
    assert not vllm_log_records('Output token throughput (tok/s): 382.89')
    assert not vllm_log_records(VLLM.rsplit('\n',1)[0])
    assert not vllm_log_records(VLLM.replace('Total token throughput (tok/s): 619.85','Output token throughput (tok/s): 619.85'))
    assert len(benchmark_markdown_records(MD.replace('7340.20 ± 23.45','7340.20–7400.0')))==5
    assert len(benchmark_markdown_records(AMD.replace('2581 t/s','-2581 t/s')))==11
    assert all(x['subject'] is None and not x['conditions_raw'] for x in vllm_log_records(HOLD))
