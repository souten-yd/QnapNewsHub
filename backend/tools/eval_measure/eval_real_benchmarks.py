"""Run: cd backend && python -m tools.eval_measure.eval_real_benchmarks

Gold compares record identity/value/stat/unit/backend/token workload.
Missing hardware/model context is not credited as a correct relation.
"""
from collections import Counter
from decimal import Decimal
from time import perf_counter
from statistics import median
import json
from tools.eval_measure.real_benchmark_cases import MODEL, JSON, CSV, MD, AMD, VLLM, HOLD
from qnapnewshub.measure_tables import llama_bench_records,markdown_measurements
from qnapnewshub.measure_benchmark_formats import benchmark_markdown_records,vllm_log_records

P='llm.prefill_tps'
D='llm.decode_tps'
json_gold=[(MODEL,P,'7100.002165','mean','tok/s','CUDA',512,None),(MODEL,D,'118.881588','mean','tok/s','CUDA',None,128)]
csv_gold=[(MODEL,P,'7285.676949','mean','tok/s','CUDA',512,None),(MODEL,D,'119.915244','mean','tok/s','CUDA',None,128)]
md_gold=[(MODEL,P,'7340.20','mean','tok/s','CUDA',512,None),(MODEL,D,'120.60','mean','tok/s','CUDA',None,128),(MODEL,P,'6425.91','mean','tok/s','CUDA',512,None),(MODEL,D,'116.71','mean','tok/s','CUDA',None,128),('llama 7B mostly Q4_0',P,'2368.80','mean','tok/s','CUDA',512,None),('llama 7B mostly Q4_0',D,'131.42','mean','tok/s','CUDA',None,128)]
amd_gold=[(s,metric,v,'unknown','tok/s',backend,512 if metric==P else None,128 if metric==D else None) for s, vals in [('Qwen2.5-3B',['2581','86.6','2089','104.9']),('Qwen2.5-7B¹',['1302','52.1','1000','54.1']),('Qwen2.5-14B',['584','18.9','417','19.2'])] for (backend,metric),v in zip([('ROCm',P),('ROCm',D),('Vulkan',P),('Vulkan',D)],vals)]
log_mets=[('llm.request_throughput_rps','unknown','req/s'),('llm.output_throughput_tps','unknown','tok/s'),('llm.total_token_throughput_tps','unknown','tok/s')]+[(f'llm.{kind}_ms',stat,'ms') for kind in ('ttft','tpot','itl') for stat in ('mean','median','p99')]


def log_gold(vals):
    return [(None,m,v,s,u,None,None,None) for (m,s,u),v in zip(log_mets,vals)]


def key(r):
    c=r['conditions_raw']
    return (r['subject'],r['metric_id'],Decimal(r['value']),r['stat'],r['unit'],c.get('backend',c.get('backends')),c.get('n_prompt') or None,c.get('n_gen') or None)


def normgold(g):
    return Counter((a,b,Decimal(c),d,e,f,h,i) for a,b,c,d,e,f,h,i in g)


def mkey(x):
    return (x.subject.text,x.metric_id,Decimal(x.value.text),'unknown',x.unit.text.casefold(),None,None,None)


CASES=[('json',JSON,lambda x:llama_bench_records(x,fmt='json'),lambda x:llama_bench_records(x,fmt='json'),json_gold),('csv',CSV,lambda x:llama_bench_records(x,fmt='csv'),lambda x:llama_bench_records(x,fmt='csv'),csv_gold),('native_markdown',MD,markdown_measurements,benchmark_markdown_records,md_gold),('backend_matrix',AMD,markdown_measurements,benchmark_markdown_records,amd_gold),('vllm_log',VLLM,markdown_measurements,vllm_log_records,log_gold(['1.73','382.89','619.85','71.54','73.88','79.49','7.91','7.96','8.03','7.74','7.70','8.39'])),('vllm_other',HOLD,markdown_measurements,vllm_log_records,log_gold(['3.83','437.46','726.48','13715.73','13254.17','26364.39','171.89','157.20','504.86','150.41','56.96','614.47']))]


def run(repeats=200):
    if not 1 <= repeats <= 1000:
        raise ValueError("invalid repeats")
    results = []
    for name,raw,before,after,g in CASES:
        gold=normgold(g)
        a=before(raw)
        b=after(raw)
        ac=Counter(key(x) if isinstance(x,dict) else mkey(x) for x in a)
        bc=Counter(key(x) for x in b)
        tp0=sum((ac&gold).values())
        tp1=sum((bc&gold).values())
        times=[]
        for _ in range(repeats):
            t=perf_counter()
            after(raw)
            times.append((perf_counter()-t)*1000)
        times.sort()
        results.append({"case":name,"gold":len(g),"before_tp":tp0,"after_tp":tp1,
                        "before_fp":len(a)-tp0,"after_fp":len(b)-tp1,
                        "p50_ms":median(times),"p95_ms":times[max(0,(95*repeats+99)//100-1)]})
    return {"cases":results,"totals":{key:sum(row[key] for row in results)
                                     for key in ("gold","before_tp","after_tp","before_fp","after_fp")}}


if __name__=="__main__":
    print(json.dumps(run(),ensure_ascii=False,indent=2))
