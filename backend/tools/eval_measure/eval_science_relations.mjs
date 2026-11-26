// Oracle span relation linking diagnostic; no extraction or runtime integration.
import {readFileSync,statSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {join} from 'node:path';
function evaluate(s,tsv) {
 const cp=[...s], nodes=new Map(), byId=new Map(), records=[];
 const lines=tsv.trimEnd().split("\n");
 if(lines.shift()!=="docId\tannotSet\tannotType\tstartOffset\tendOffset\tannotId\ttext\tother")throw Error("unexpected header");
 for(const line of lines){const [doc,group,type,a,b,id,raw,other]=line.split("\t");const start=+a,end=+b;
  if(!Number.isInteger(start)||!Number.isInteger(end)||start<0||end<=start||cp.slice(start,end).join("")!==raw)throw Error("invalid offset");
  const key=`${type}:${start}:${end}`;const node={key,type,start,end};nodes.set(key,node);byId.set(group+":"+id,node);records.push({group,node,other:other?JSON.parse(other):{}});}
 const gold=new Map(), types=["HasQuantity","HasProperty","Qualifies"];
 const link=(source,type,target)=>`${source.key}|${type}|${target.key}`;
 for(const {group,node,other} of records)for(const type of types)if(type in other){const target=byId.get(group+":"+other[type]);if(!target)throw Error("dangling target");gold.set(link(node,type,target),type);}
 const choices=(source,target)=>source.type==="MeasuredProperty"&&target.type==="Quantity"?"HasQuantity":source.type==="MeasuredEntity"&&target.type==="Quantity"?"HasQuantity":source.type==="MeasuredEntity"&&target.type==="MeasuredProperty"?"HasProperty":source.type==="Qualifier"&&["Quantity","MeasuredProperty","MeasuredEntity"].includes(target.type)?"Qualifies":null;
 const gap=(a,b)=>Math.max(0,a.start-b.end,b.start-a.end);
 const boundary=(a,b)=>/[.!?]\s+(?=[A-Z])/.test(cp.slice(Math.min(a.end,b.end),Math.max(a.start,b.start)).join(""));
 const out={gold:gold.size,types:Object.fromEntries(types.map(t=>[t,[...gold.values()].filter(x=>x===t).length])),methods:{}};
 for(const method of ["nearest","sentence_first"]){const pred=new Map();for(const a of nodes.values()){const opts=[...nodes.values()].map(b=>({b,t:choices(a,b)})).filter(x=>x.t);
  opts.sort((x,y)=>(method==="sentence_first"?Number(boundary(a,x.b))-Number(boundary(a,y.b)):0)||gap(a,x.b)-gap(a,y.b)||Math.abs(a.start+a.end-x.b.start-x.b.end)-Math.abs(a.start+a.end-y.b.start-y.b.end)||x.b.start-y.b.start||x.b.end-y.b.end||(x.b.key<y.b.key?-1:x.b.key>y.b.key?1:0));
  if(opts.length){const {b,t}=opts[0];pred.set(link(a,t,b),t);}}
 const tp=[...pred.keys()].filter(k=>gold.has(k));out.methods[method]={pred:pred.size,tp:tp.length,by_type:Object.fromEntries(types.map(t=>[t,{pred:[...pred.values()].filter(x=>x===t).length,tp:tp.filter(k=>pred.get(k)===t).length}]))};}
 return out;
}
const samples=[
  {
    "id": "S0032063312002437-586",
    "sha": [
      "2b873976e75ac8f36d06ea36b1b14d0093e32701",
      "c49bee69fe2c28453fe4edf15ee8df82fbc1c9b0"
    ]
  },
  {
    "id": "S0167577X13006393-801",
    "sha": [
      "25c91163a5520e411bfd398262aa152df8eed43d",
      "022373a08fa50fd116baf349410473b745a3b6fa"
    ]
  },
  {
    "id": "S0925443913001385-849",
    "sha": [
      "155d5b9d2c2d6321a4e3d1be8d3cf0aebdd6d81e",
      "be347ee37f203c8531179bdcc3c0b7621ddfb6b3"
    ]
  },
  {
    "id": "S1873506114000075-665",
    "sha": [
      "7eaaf743b25de5066129fa4ffd5bd0f06ebdfb2c",
      "ea5fad295381552aaa3872c06d7849970526f181"
    ]
  },
  {
    "id": "S2211124713006475-741",
    "sha": [
      "190b32bad41b5d3396c40d7ec66a3f5374429da6",
      "671b75ac4401227b02c17f854667b926f956bfd5"
    ]
  },
  {
    "id": "S0960148113005727-739",
    "sha": [
      "54236064c9a6a0b8c2245df07faf9216d156ef6a",
      "9bd467a9ced83f385fbbb294f2525c582acb3ef3"
    ]
  },
  {
    "id": "S0022399913003358-956",
    "sha": [
      "15145613c56302c444e8c2fd9e3d300b763289d9",
      "32149bec4e83be0e8d188d3d582430184d9b4dd3"
    ]
  },
  {
    "id": "S0022459611006116-987",
    "sha": [
      "60b7d51ab2c0d4d82342df1a5ad14185bf2fe4ca",
      "9e78274c63a4de6a7363dedf4ddf1928b0c97710"
    ]
  }
];
function verified(p,sha){if(statSync(p).size>1024*1024)throw Error('sample too large');const b=readFileSync(p);if(createHash('sha1').update('blob '+b.length+'\0').update(b).digest('hex')!==sha)throw Error('source blob mismatch');return b.toString('utf8');}
const root=process.argv[2];if(!root)throw Error('supply MeasEval checkout');
console.log(JSON.stringify(samples.map(({id,sha})=>({id,...evaluate(verified(join(root,'data','eval','text',id+'.txt'),sha[0]),verified(join(root,'data','eval','tsv',id+'.tsv'),sha[1]))})),null,2));
