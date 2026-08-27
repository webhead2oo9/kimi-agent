import fs from "node:fs/promises";

import { buildAxisScale, formatScaleLabel, layoutOverlapBadge } from "./visual_math.mjs";

const RESULT_PREFIX = "__VISUAL_RESULT__";
const MAX_INPUT_BYTES = 256 * 1024;
const MAX_OUTPUT_BYTES = Number(process.env.VISUAL_MAX_OUTPUT_BYTES);
const CALL_TIMEOUT_SECONDS = Number(process.env.VISUAL_CALL_TIMEOUT_SECONDS);
const WIDTH = 1200;
const HEIGHT = 675;
const MAX_ABS_VALUE = 1_000_000_000_000_000;
const modulePath = String(process.env.BETTERWRIGHT_MODULE || "").trim();
const mermaidPath = String(process.env.MERMAID_BUNDLE || "").trim();
if (!modulePath || !mermaidPath) throw new Error("visual runtime is not configured");
if (!Number.isSafeInteger(MAX_OUTPUT_BYTES) || MAX_OUTPUT_BYTES <= 0) {
  throw new Error("visual output limit is invalid");
}
if (!Number.isFinite(CALL_TIMEOUT_SECONDS) || CALL_TIMEOUT_SECONDS <= 0) {
  throw new Error("visual timeout is invalid");
}

const { BetterWright, NetworkPolicy } = await import(modulePath);
const browser = new BetterWright({
  home: "/work",
  headless: true,
  vault: false,
  credentialCapture: false,
  downloadPolicy: "deny",
  publicSearchPolicy: "block",
  liveView: false,
  policy: new NetworkPolicy({
    allowPrivateNetwork: false,
    allowLoopback: false,
    custom: () => ({ allowed: false, reason: "visual renderer is offline" }),
  }),
});

function emit(value) {
  const encoded = JSON.stringify(value);
  process.stdout.write(`${RESULT_PREFIX}${encoded.slice(0, 16384)}\n`);
}

function assertObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
}

function assertKeys(value, allowed, name) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length) throw new Error(`${name} contains unknown fields`);
}

function assertText(value, name, maximum, required = false) {
  if (typeof value !== "string" || (required && !value.trim()) || value.length > maximum) {
    throw new Error(`${name} is invalid`);
  }
}

function validateRequest(value) {
  assertObject(value, "visual request");
  assertText(value.title, "title", 200);
  assertText(value.alt_text, "alt_text", 1000, true);
  if (value.kind === "mermaid") {
    assertKeys(value, new Set(["kind", "title", "alt_text", "source"]), "Mermaid request");
    assertText(value.source, "source", 12000, true);
    if ([...value.source].some((char) => char.charCodeAt(0) < 32 && !['\n', '\t'].includes(char))) {
      throw new Error("source contains unsupported control characters");
    }
    const lines = value.source.replace(/\r\n?/g, '\n').split('\n');
    if (lines.length > 300) throw new Error("source has too many lines");
    if (value.source.startsWith('---') || lines.some((line) => line.trim() === '---')) {
      throw new Error("Mermaid frontmatter is not allowed");
    }
    const header = lines.find((line) => line.trim() && !line.trimStart().startsWith('%%'))?.trim() || '';
    if (!/^(?:(?:flowchart|graph)\s+(?:TB|TD|BT|RL|LR)|sequenceDiagram|stateDiagram-v2|classDiagram|erDiagram)$/i.test(header)) {
      throw new Error("source has an unsupported Mermaid header");
    }
    if (/(?:%%\{|\bclick\b|\bhref\b|\b(?:https?|wss?|ftp):\/\/|\b(?:data|file):|\bwww\.|\burl\s*\(|<\s*\/?\s*[a-z!]|@\{|\b(?:img|icon)\s*:|^\s*(?:style|classDef|linkStyle|theme|font)\b|\bfont-(?:family|size|style|weight)\b|\bcss\b|:::)/im.test(value.source)) {
      throw new Error("source contains forbidden Mermaid content");
    }
    return { ...value, source: lines.join('\n').trim() };
  }
  if (value.kind !== "chart") throw new Error("kind is invalid");
  assertKeys(
    value,
    new Set([
      "kind", "title", "alt_text", "chart_type", "x_label", "y_label",
      "x_scale", "y_scale", "overlap_mode", "categories", "series",
    ]),
    "chart request",
  );
  if (!["bar", "line", "scatter"].includes(value.chart_type)) throw new Error("chart type is invalid");
  if (!["linear", "symlog"].includes(value.x_scale)) throw new Error("x scale is invalid");
  if (!["linear", "symlog"].includes(value.y_scale)) throw new Error("y scale is invalid");
  if (!["none", "count"].includes(value.overlap_mode)) throw new Error("overlap mode is invalid");
  if (
    value.chart_type !== "scatter" &&
    (value.x_scale !== "linear" || value.y_scale !== "linear" || value.overlap_mode !== "none")
  ) throw new Error("scatter controls are invalid for this chart type");
  assertText(value.x_label, "x label", 100);
  assertText(value.y_label, "y label", 100);
  if (!Array.isArray(value.categories) || value.categories.length > 250) throw new Error("categories are invalid");
  if (!Array.isArray(value.series) || !value.series.length || value.series.length > 8) throw new Error("series are invalid");
  if (value.chart_type === "scatter" && value.categories.length) throw new Error("scatter categories are invalid");
  if (value.chart_type !== "scatter" && !value.categories.length) throw new Error("categories are required");
  if (value.chart_type === "bar" && value.categories.length > 50) throw new Error("bar categories are invalid");
  value.categories.forEach((label) => assertText(label, "category", 100, true));
  let total = 0;
  value.series.forEach((series) => {
    assertObject(series, "series");
    assertText(series.name, "series name", 100, true);
    if (value.chart_type === "scatter") {
      assertKeys(series, new Set(["name", "points"]), "scatter series");
      if (!Array.isArray(series.points) || !series.points.length || series.points.length > 250) throw new Error("scatter points are invalid");
      series.points.forEach((point) => {
        assertObject(point, "point");
        assertKeys(point, new Set(["x", "y", "label"]), "point");
        if (
          !Number.isFinite(point.x) ||
          !Number.isFinite(point.y) ||
          Math.abs(point.x) > MAX_ABS_VALUE ||
          Math.abs(point.y) > MAX_ABS_VALUE
        ) throw new Error("point is invalid");
        assertText(point.label, "point label", 100);
      });
      total += series.points.length;
    } else {
      assertKeys(series, new Set(["name", "values"]), "chart series");
      if (
        !Array.isArray(series.values) ||
        series.values.length !== value.categories.length ||
        series.values.length > 250 ||
        !series.values.every((item) => Number.isFinite(item) && Math.abs(item) <= MAX_ABS_VALUE)
      ) throw new Error("series values are invalid");
      total += series.values.length;
    }
  });
  if (total > 1000) throw new Error("visual has too many points");
  return value;
}

async function readRequest() {
  const chunks = [];
  let total = 0;
  for await (const chunk of process.stdin) {
    total += chunk.length;
    if (total > MAX_INPUT_BYTES) throw new Error("visual request is too large");
    chunks.push(chunk);
  }
  return validateRequest(JSON.parse(Buffer.concat(chunks).toString("utf8")));
}

function renderCode(request, mermaidBundle) {
  const input = JSON.stringify(request);
  const bundle = JSON.stringify(mermaidBundle);
  return `
const INPUT = ${input};
const MERMAID_BUNDLE = ${bundle};
const WIDTH = ${WIDTH};
const HEIGHT = ${HEIGHT};
await page.setViewportSize({ width: WIDTH, height: HEIGHT });
await page.setContent(\`<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}html,body{margin:0;padding:0;background:#fff;color:#111;font-family:Arial,sans-serif}
#visual{width:1200px;height:675px;overflow:hidden;background:#fff;display:flex;flex-direction:column}
#title{height:52px;flex:0 0 52px;display:flex;align-items:center;justify-content:center;padding:8px 28px 4px;font-size:26px;font-weight:700;line-height:1.15;text-align:center}
#diagram{width:1200px;height:623px;display:flex;align-items:center;justify-content:center;padding:12px 22px 20px}
#diagram svg{display:block;max-width:1156px!important;max-height:591px!important;width:auto!important;height:auto!important}
</style></head><body><main id="visual" role="img"><div id="title"></div><div id="diagram"></div></main></body></html>\`);
await page.evaluate(({ altText, title }) => {
  document.querySelector('#visual').setAttribute('aria-label', altText);
  document.querySelector('#title').textContent = title || '';
}, { altText: INPUT.alt_text, title: INPUT.title });
if (INPUT.kind === 'mermaid') {
  await page.addScriptTag({ content: MERMAID_BUNDLE });
  await page.evaluate(async (source) => {
    if (!globalThis.mermaid) throw new Error('Mermaid runtime did not initialize');
    globalThis.mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'base',
      htmlLabels: false,
      deterministicIds: true,
      deterministicIDSeed: 'kimi-visual',
      maxTextSize: 12000,
      themeVariables: {
        background: '#ffffff', primaryColor: '#e8f1f8', primaryTextColor: '#111111',
        primaryBorderColor: '#111111', lineColor: '#222222', secondaryColor: '#f5f5f5',
        tertiaryColor: '#ffffff', fontFamily: 'Arial, sans-serif'
      }
    });
    const rendered = await globalThis.mermaid.render('kimi-mermaid', source);
    const parsed = new DOMParser().parseFromString(rendered.svg, 'image/svg+xml');
    if (parsed.querySelector('parsererror')) throw new Error('Mermaid produced invalid SVG');
    const svg = parsed.documentElement;
    if (svg.localName !== 'svg') throw new Error('Mermaid produced no SVG');
    if (svg.querySelector('script, foreignObject, image, iframe, object, embed, link')) {
      throw new Error('Mermaid produced forbidden SVG content');
    }
    for (const element of [svg, ...svg.querySelectorAll('*')]) {
      for (const attribute of [...element.attributes]) {
        const name = attribute.name.toLowerCase();
        const value = attribute.value.trim();
        if (name.startsWith('on')) throw new Error('Mermaid produced an event handler');
        if ((name === 'href' || name === 'xlink:href') && value && !value.startsWith('#')) {
          throw new Error('Mermaid produced an external reference');
        }
        if (
          (name === 'style' || element.tagName.toLowerCase() === 'style') &&
          (/expression\\s*\\(/i.test(value) || /@import/i.test(value) || /url\\s*\\(\\s*(?!#)/i.test(value))
        ) {
          throw new Error('Mermaid produced unsafe CSS');
        }
      }
      if (element.tagName.toLowerCase() === 'style') {
        const css = element.textContent || '';
        if (/expression\\s*\\(/i.test(css) || /@import/i.test(css) || /url\\s*\\(\\s*(?!#)/i.test(css)) {
          throw new Error('Mermaid produced unsafe CSS');
        }
      }
    }
    svg.removeAttribute('style');
    svg.setAttribute('role', 'presentation');
    const host = document.querySelector('#diagram');
    host.replaceChildren(document.importNode(svg, true));
  }, INPUT.source);
} else {
  await page.evaluate((data) => {
    ${buildAxisScale.toString()}
    ${formatScaleLabel.toString()}
    ${layoutOverlapBadge.toString()}
    const NS = 'http://www.w3.org/2000/svg';
    const palette = ['#E69F00','#56B4E9','#009E73','#F0E442','#0072B2','#D55E00','#CC79A7','#000000'];
    const dashes = ['', '12 6', '4 5', '16 5 3 5', '2 4', '10 4 2 4 2 4', '18 7', '7 3'];
    const markers = ['circle','square','triangle','diamond','cross','plus','star','down'];
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', '1200'); svg.setAttribute('height', '623');
    svg.setAttribute('viewBox', '0 0 1200 623');
    svg.style.display = 'block';
    const add = (tag, attrs = {}, text = '') => {
      const node = document.createElementNS(NS, tag);
      for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
      if (text) node.textContent = text;
      svg.appendChild(node); return node;
    };
    const defs = add('defs');
    palette.forEach((color, index) => {
      const pattern = document.createElementNS(NS, 'pattern');
      pattern.setAttribute('id', 'hatch-' + index); pattern.setAttribute('width', '10');
      pattern.setAttribute('height', '10'); pattern.setAttribute('patternUnits', 'userSpaceOnUse');
      const bg = document.createElementNS(NS, 'rect'); bg.setAttribute('width','10'); bg.setAttribute('height','10'); bg.setAttribute('fill',color); pattern.appendChild(bg);
      const path = document.createElementNS(NS, 'path');
      path.setAttribute('d', index % 3 === 0 ? 'M0 10L10 0' : index % 3 === 1 ? 'M0 0L10 10' : 'M5 0V10');
      path.setAttribute('stroke','#111'); path.setAttribute('stroke-width','1.4'); path.setAttribute('opacity','0.55'); pattern.appendChild(path);
      defs.appendChild(pattern);
    });
    const markerPath = (shape, x, y, size, color) => {
      if (shape === 'circle') return add('circle',{cx:x,cy:y,r:size,fill:color,stroke:'#111','stroke-width':1.5});
      if (shape === 'square') return add('rect',{x:x-size,y:y-size,width:size*2,height:size*2,fill:color,stroke:'#111','stroke-width':1.5});
      const paths = {
        triangle: \`M \${x} \${y-size} L \${x+size} \${y+size} L \${x-size} \${y+size} Z\`,
        diamond: \`M \${x} \${y-size} L \${x+size} \${y} L \${x} \${y+size} L \${x-size} \${y} Z\`,
        cross: \`M \${x-size} \${y-size} L \${x+size} \${y+size} M \${x+size} \${y-size} L \${x-size} \${y+size}\`,
        plus: \`M \${x-size} \${y} H \${x+size} M \${x} \${y-size} V \${y+size}\`,
        star: \`M \${x} \${y-size} L \${x+size*.35} \${y-size*.25} L \${x+size} \${y-size*.2} L \${x+size*.5} \${y+size*.25} L \${x+size*.62} \${y+size} L \${x} \${y+size*.58} L \${x-size*.62} \${y+size} L \${x-size*.5} \${y+size*.25} L \${x-size} \${y-size*.2} L \${x-size*.35} \${y-size*.25} Z\`,
        down: \`M \${x-size} \${y-size} L \${x+size} \${y-size} L \${x} \${y+size} Z\`
      };
      return add('path',{d:paths[shape],fill:['cross','plus'].includes(shape)?'none':color,stroke:color,'stroke-width':3,'stroke-linecap':'round','stroke-linejoin':'round'});
    };
    const left=105, right=1160, top=76, bottom=520, plotW=right-left, plotH=bottom-top;
    const tickLabel=value=>{
      const absolute=Math.abs(value);
      if(absolute>=1e6||(absolute>0&&absolute<0.001)) return value.toExponential(2);
      return Number(value.toPrecision(4)).toString();
    };
    const all = data.chart_type === 'scatter'
      ? data.series.flatMap(s => s.points.map(p => p.y)) : data.series.flatMap(s => s.values);
    const yAxis=buildAxisScale(all,bottom,top,data.chart_type==='scatter'?data.y_scale:'linear',true);
    const y=yAxis.position;
    const text = (x,yPos,value,attrs={}) => add('text',{x,y:yPos,fill:'#111','font-size':16,'font-family':'Arial, sans-serif',...attrs},value);
    yAxis.ticks.forEach((value,tick)=>{
      const py=y(value);
      const isZero=value===0;
      add('line',{x1:left,y1:py,x2:right,y2:py,stroke:'#777','stroke-width':isZero?1.5:1,'stroke-dasharray':isZero?'':'3 5'});
      text(left-12,py+5,tickLabel(value),{'text-anchor':'end','font-size':14});
    });
    add('line',{x1:left,y1:top,x2:left,y2:bottom,stroke:'#111','stroke-width':2});
    add('line',{x1:left,y1:bottom,x2:right,y2:bottom,stroke:'#111','stroke-width':2});
    if(data.chart_type === 'bar'){
      const group=plotW/data.categories.length, barW=Math.max(1,Math.min(42,group*.78/data.series.length));
      data.categories.forEach((category,ci)=>{
        const center=left+group*(ci+.5);
        data.series.forEach((series,si)=>{
          const value=series.values[ci], zero=y(0), py=y(value);
          add('rect',{x:center-(data.series.length*barW)/2+si*barW,y:Math.min(zero,py),width:Math.max(1,barW-1),height:Math.max(1,Math.abs(zero-py)),fill:\`url(#hatch-\${si})\`,stroke:'#111','stroke-width':1});
        });
        const label=text(center,bottom+18,category,{'text-anchor':data.categories.length>12?'end':'middle','font-size':data.categories.length>30?10:12});
        if(data.categories.length>12) label.setAttribute('transform',\`rotate(-45 \${center} \${bottom+18})\`);
      });
    } else if(data.chart_type === 'line'){
      const x=i=>left+(data.categories.length===1?plotW/2:i*plotW/(data.categories.length-1));
      const labelStep = Math.max(1, Math.ceil(data.categories.length / 12));
      data.categories.forEach((category,i)=>{
        if (i % labelStep === 0 || i === data.categories.length - 1) {
          text(x(i),bottom+22,category,{'text-anchor':'middle','font-size':12});
        }
      });
      data.series.forEach((series,si)=>{
        const points=series.values.map((value,i)=>\`\${x(i)},\${y(value)}\`).join(' ');
        add('polyline',{points,fill:'none',stroke:palette[si],'stroke-width':4,'stroke-dasharray':dashes[si]});
        series.values.forEach((value,i)=>markerPath(markers[si],x(i),y(value),6,palette[si]));
      });
    } else {
      const xs=data.series.flatMap(s=>s.points.map(p=>p.x));
      const xAxis=buildAxisScale(xs,left,right,data.x_scale,false), x=xAxis.position;
      xAxis.ticks.forEach(value=>text(x(value),bottom+22,tickLabel(value),{'text-anchor':'middle','font-size':13}));
      const overlaps=new Map();
      data.series.forEach(series=>series.points.forEach(point=>{
        const key=JSON.stringify([point.x,point.y]);
        const current=overlaps.get(key)||{point,count:0};current.count+=1;overlaps.set(key,current);
      }));
      data.series.forEach((series,si)=>series.points.forEach(point=>{
        const px=x(point.x), py=y(point.y);
        markerPath(markers[si],px,py,8,palette[si]);
        if(point.label) {
          const placeLeft=px>right-160;
          const overlap=overlaps.get(JSON.stringify([point.x,point.y]));
          const labelWidth=16+String(overlap.count).length*8;
          const layout=overlap.count>1&&data.overlap_mode==='count'
            ? layoutOverlapBadge(px,py,left,right,top,bottom,labelWidth,true):null;
          text(px+(placeLeft?-11:11),layout?layout.pointLabelY:py-10,point.label,{'text-anchor':placeLeft?'end':'start','font-size':12});
        }
      }));
      if(data.overlap_mode==='count'){
        overlaps.forEach(({point,count})=>{
          if(count<2)return;
          const px=x(point.x),py=y(point.y),label='x'+count,labelWidth=16+String(count).length*8;
          const hasLabel=data.series.some(series=>series.points.some(candidate=>candidate.x===point.x&&candidate.y===point.y&&candidate.label));
          const layout=layoutOverlapBadge(px,py,left,right,top,bottom,labelWidth,hasLabel);
          add('rect',{x:layout.x,y:layout.y,width:labelWidth,height:19,rx:8,fill:'#fff',stroke:'#111','stroke-width':1.5});
          text(layout.x+labelWidth/2,layout.y+14,label,{'text-anchor':'middle','font-size':12,'font-weight':'700'});
        });
      }
    }
    const xLabel=formatScaleLabel(data.x_label,data.chart_type==='scatter'?data.x_scale:'linear');
    const yLabel=formatScaleLabel(data.y_label,data.chart_type==='scatter'?data.y_scale:'linear');
    if(xLabel) text((left+right)/2,608,xLabel,{'text-anchor':'middle','font-size':18,'font-weight':'700'});
    if(yLabel){const label=text(25,(top+bottom)/2,yLabel,{'text-anchor':'middle','font-size':18,'font-weight':'700'});label.setAttribute('transform',\`rotate(-90 25 \${(top+bottom)/2})\`);}
    const legendColumns=Math.min(4,data.series.length), legendCell=plotW/legendColumns;
    data.series.forEach((series,si)=>{
      const legendX=left+(si%legendColumns)*legendCell, legendY=18+Math.floor(si/legendColumns)*25;
      if(data.chart_type === 'bar') {
        add('rect',{x:legendX-6,y:legendY-6,width:12,height:12,fill:\`url(#hatch-\${si})\`,stroke:'#111','stroke-width':1});
      } else {
        markerPath(markers[si],legendX,legendY,6,palette[si]);
      }
      const legendName=series.name.length>24?series.name.slice(0,21)+'...':series.name;
      text(legendX+13,legendY+5,legendName,{'font-size':14});
    });
    document.querySelector('#diagram').replaceChildren(svg);
  }, INPUT);
}
const shot = await screenshot({
  kind: 'debug',
  name: 'render.png',
  type: 'png',
  fullPage: false,
  annotate: false,
});
return { output: shot.path };
`;
}

try {
  const request = await readRequest();
  const mermaidBundle = request.kind === "mermaid" ? await fs.readFile(mermaidPath, "utf8") : "";
  const result = await browser.run(renderCode(request, mermaidBundle), {
    session: "visual",
    timeout: CALL_TIMEOUT_SECONDS,
  });
  if (!result?.ok || typeof result.result?.output !== "string") {
    throw new Error(String(result?.error || "render failed"));
  }
  const artifact = result.result.output;
  if (!artifact.startsWith("/work/artifacts/") || !artifact.toLowerCase().endsWith(".png")) {
    throw new Error("renderer returned an invalid artifact path");
  }
  const artifactInfo = await fs.lstat(artifact);
  const resolvedArtifact = await fs.realpath(artifact);
  if (
    !artifactInfo.isFile() ||
    artifactInfo.isSymbolicLink() ||
    !resolvedArtifact.startsWith("/work/artifacts/")
  ) {
    throw new Error("renderer returned an unsafe artifact path");
  }
  const bytes = await fs.readFile(resolvedArtifact);
  if (bytes.length < 24 || bytes.length > MAX_OUTPUT_BYTES || bytes.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a") {
    throw new Error("renderer produced an invalid PNG");
  }
  const width = bytes.readUInt32BE(16);
  const height = bytes.readUInt32BE(20);
  await fs.writeFile("/output/render.png", bytes, { mode: 0o600, flag: "wx" });
  emit({ ok: true, filename: "render.png", width, height });
} catch (error) {
  emit({ ok: false, error: String(error?.message || error).slice(0, 2000) });
  process.exitCode = 1;
} finally {
  await browser.close();
}
