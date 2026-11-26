"""Published benchmark examples, selected numeric facts, NOT full source documents.

JSON/CSV contain projected fields from original examples; Markdown whitespace
is normalized for native tables; logs retain only measurement labels and
values inside the original result delimiters. The gold is independently
written in eval_real_benchmarks.py. This small format coverage set is not
an unbiased retrieval or extraction benchmark.
"""
import json,csv,io
MODEL='qwen2 7B Q4_K - Medium'
common={'model_type':MODEL,'model_filename':'models/Qwen2.5-7B-Instruct-Q4_K_M.gguf','gpu_info':'NVIDIA GeForce RTX 4080','cpu_info':'AMD Ryzen 7 7800X3D 8-Core Processor','backends':'CUDA','build_commit':'8cf427ff','n_threads':8,'n_batch':2048,'n_ubatch':512,'n_gpu_layers':-1,'type_k':'f16','type_v':'f16','flash_attn':-1}
JSON=json.dumps([{**common,'n_prompt':512,'n_gen':0,'avg_ts':7100.002165,'stddev_ts':140.341520},{**common,'n_prompt':0,'n_gen':128,'avg_ts':118.881588,'stddev_ts':1.041811}])
# Only numeric/identity fields used for evaluation are projected. No claim
# that every non-tested CSV field equals the separate JSON run.
rows=[{**common,'n_prompt':512,'n_gen':0,'avg_ts':7285.676949,'stddev_ts':100.064434},{**common,'n_prompt':0,'n_gen':128,'avg_ts':119.915244,'stddev_ts':0.430617}]
f=io.StringIO(); w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows); CSV=f.getvalue()
MD='''| model | size | params | backend | ngl | test | t/s |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| qwen2 7B Q4_K - Medium | 4.36 GiB | 7.62 B | CUDA | -1 | pp512 | 7340.20 ± 23.45 |
| qwen2 7B Q4_K - Medium | 4.36 GiB | 7.62 B | CUDA | -1 | tg128 | 120.60 ± 0.59 |
| qwen2 7B Q4_K - Medium | 4.36 GiB | 7.62 B | CUDA | -1 | pp512 @ d512 | 6425.91 ± 18.88 |
| qwen2 7B Q4_K - Medium | 4.36 GiB | 7.62 B | CUDA | -1 | tg128 @ d512 | 116.71 ± 0.60 |

| model | size | params | backend | ngl | test | t/s |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| llama 7B mostly Q4_0 | 3.56 GiB | 6.74 B | CUDA | -1 | pp 512 | 2368.80 ± 93.24 |
| llama 7B mostly Q4_0 | 3.56 GiB | 6.74 B | CUDA | -1 | tg 128 | 131.42 ± 0.59 |'''
AMD='''| Model | File | Fits 8 GB? | ngl | ROCm pp512 | ROCm tg128 | Vulkan pp512 | Vulkan tg128 |
|---|---:|:--:|--:|---:|---:|---:|---:|
| Qwen2.5-3B  | 1.79 GiB | ✅ full  | 99 | 2581 t/s | 86.6 t/s | 2089 t/s | **104.9 t/s** |
| Qwen2.5-7B¹ | 4.36 GiB | ✅ full  | 99 | 1302 t/s | 52.1 t/s | 1000 t/s | 54.1 t/s |
| Qwen2.5-14B | 8.37 GiB | ⚠️ partial | 44 | 584 t/s | 18.9 t/s | 417 t/s | 19.2 t/s |'''
LABELS=['Request throughput (req/s)','Output token throughput (tok/s)','Total token throughput (tok/s)','Mean TTFT (ms)','Median TTFT (ms)','P99 TTFT (ms)','Mean TPOT (ms)','Median TPOT (ms)','P99 TPOT (ms)','Mean ITL (ms)','Median ITL (ms)','P99 ITL (ms)']
def log(values):
    return '============ Serving Benchmark Result ============\n'+'\n'.join(f'{k}: {v}' for k,v in zip(LABELS,values))+'\n=================================================='
VLLM=log(['1.73','382.89','619.85','71.54','73.88','79.49','7.91','7.96','8.03','7.74','7.70','8.39'])
HOLD=log(['3.83','437.46','726.48','13715.73','13254.17','26364.39','171.89','157.20','504.86','150.41','56.96','614.47'])
