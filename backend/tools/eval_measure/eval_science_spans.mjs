// Offline candidate-only science quantity span diagnostic. No runtime integration.
import {readFileSync,statSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {join} from 'node:path';
function candidates(s) {
 const N=String.raw`[+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+\-−]?\d{1,3})?`;
 const U=String.raw`(?:cm[²2]/Vs|(?:ng|µg|μg|mg|g|kg)/(?:ml|mL|L|l)|wt%|ppm|‰|%|×|(?:nm|μm|µm|mm|cm|km|m|kg|mg|g|K|°C|kHz|MHz|Hz|mH|μH|µH|H|kV|mV|V|mA|A|kW|W|ms|hr|hours?|days?|years?|s|h)(?![A-Za-z0-9]))`;
 const P=String.raw`(?:(?:up to|at least|below|above|less than|more than|after|before)\s+)?(?:(?:approximately|about|around)\s+|[~∼≈]\s*)?`;
 const numeric=String.raw`(?:${N})(?:\s*(?:±|\+|–|—|−|-|\bto\b|:)\s*(?:${N}))?`;
 const re=new RegExp(String.raw`(?<![\p{L}\p{N}_.])(?:${P}(?:[<>≤≥]\s*)?${numeric}\s*${U}|${P}${N}\s*:\s*${N}|(?<=\bn\s*=\s*)${N}|${N}(?=\s+classes\b))(?![\p{L}\p{N}])`,"gu");
 return [...s.matchAll(re)].map(m=>({start:[...s.slice(0,m.index)].length,end:[...s.slice(0,m.index+m[0].length)].length,raw:m[0]}));
}
function assess(s,tsv) {
 const cp=[...s];const gold=tsv.trimEnd().split("\n").slice(1).map(x=>x.split("\t")).filter(x=>x[2]==="Quantity").map(x=>({start:+x[3],end:+x[4],raw:x[6]}));
 if(!gold.every(g=>cp.slice(g.start,g.end).join("")===g.raw))throw Error("annotation offset mismatch");
 const pred=candidates(s),eq=(p,g)=>p.start===g.start&&p.end===g.end,ov=(p,g)=>p.start<g.end&&g.start<p.end;
 // Exact spans counted one-to-one, even if annotations repeat.
 let used=new Set(),exact=0;
 for(const p of pred){let i=gold.findIndex((g,i)=>!used.has(i)&&eq(p,g));if(i>=0){used.add(i);exact++;}}
 return {gold:gold.length,pred:pred.length,exact,covered:gold.filter(g=>pred.some(p=>ov(p,g))).length,unmatched_pred:pred.filter(p=>!gold.some(g=>eq(p,g))).map(p=>({start:p.start,end:p.end})),unmatched_gold:gold.filter(g=>!pred.some(p=>eq(p,g))).map(g=>({start:g.start,end:g.end}))};
}
const samples=[
  {
    "id": "S2213671113001306-910",
    "split": "train",
    "sha": [
      "487758e277a68fdedeb2ef8425f03740e4c701a4",
      "914370d339261906de0dcc766aaba17718339025"
    ]
  },
  {
    "id": "S0927024813001955-679",
    "split": "train",
    "sha": [
      "e4a56ca4e8032d4679f5e6d76fb0d06f3a4870ca",
      "689a4d97022160e7ccfba1d84ca7a7ae235e0e73"
    ]
  },
  {
    "id": "S1161030113001950-923",
    "split": "train",
    "sha": [
      "cbefe5bae965f6ae2af7cb9f054f105341bd8112",
      "ba429d71a552f862cc15580d3444f1e798918da1"
    ]
  },
  {
    "id": "S2213671113000738-667",
    "split": "eval",
    "sha": [
      "4e4dd37f25c6bdea8e3f1e6793266e8f31d6e7d9",
      "7109af66b9424185b29627599c5aaf898832ae8b"
    ]
  },
  {
    "id": "S2213671113000921-756",
    "split": "eval",
    "sha": [
      "d39b31aa115440ac1e5a152918330c9465424794",
      "9514f43f0bb50700e75c842c8be1293076fb2d2e"
    ]
  },
  {
    "id": "S0038071711004354-755",
    "split": "eval",
    "sha": [
      "747122b28cda1731f1edf53c136e34b63f28275d",
      "45bac97aa5c2774f05d0974c1c1b358d25c0a3c9"
    ]
  },
  {
    "id": "S0257897213007573-959",
    "split": "eval",
    "sha": [
      "a075e9e239cc0681d652db28d63f60114d370a67",
      "7b5f471c3edde43ec92ca45c9613cc63ccbc325d"
    ]
  },
  {
    "id": "S2211124712002884-620",
    "split": "eval",
    "sha": [
      "9450b2b8765b8ab02ee1e5f5654ce66f85d49e92",
      "1443a6f82145863914f436a5ea77ed0e74309a7b"
    ]
  },
  {
    "id": "S0301010413004096-693",
    "split": "eval",
    "sha": [
      "688a9342d23de7c3bc628e99bfac02023d7284a4",
      "08d208ec79f5fb5ff98b4251a1e9fa3508fc48db"
    ]
  }
];
function verified(p,sha){if(statSync(p).size>1024*1024)throw Error('sample too large');const b=readFileSync(p);if(createHash('sha1').update('blob '+b.length+'\0').update(b).digest('hex')!==sha)throw Error('source blob mismatch');return b.toString('utf8');}
const root=process.argv[2];if(!root)throw Error('supply MeasEval checkout');
const rows=samples.map(({id,split,sha})=>({id,split,...assess(verified(join(root,'data',split,'text',id+'.txt'),sha[0]),verified(join(root,'data',split,'tsv',id+'.tsv'),sha[1]))}));
console.log(JSON.stringify(rows,null,2));
