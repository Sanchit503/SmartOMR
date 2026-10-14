"""Self-contained member and ensemble report; no server or plotting dependency."""
from __future__ import annotations

import base64
from html import escape
import json
from pathlib import Path


def write_visual_report(path: Path, report: dict, ensemble_rows: list[dict],
                        member_rows: list[dict], dataset: Path) -> None:
    groups = []
    all_rows = member_rows + [{"model": "ensemble", **row} for row in ensemble_rows]
    crops = {}
    for row in all_rows:
        if not row["correct"] and row["image_path"] not in crops:
            image = (dataset / row["image_path"].replace("\\", "/")).resolve()
            if not image.is_relative_to(dataset.resolve()):
                raise ValueError("Report crop outside dataset")
            crops[row["image_path"]] = "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode("ascii")
    for group in report["results"]:
        models = []
        for model in [*group["members"], {"model": "ensemble", **group}]:
            model_id = model["model"]
            errors = [dict(row, probabilities=json.loads(row["probabilities"])) for row in all_rows
                      if row["ensemble"] == group["name"] and row["model"] == model_id and not row["correct"]]
            models.append({**model, "errors": errors})
        groups.append({"name": group["name"], "exposure": group["exposure"], "models": models})
    payload = {"samples": report["samples"], "warning": report["warning"], "groups": groups, "crops": crops,
               "scope": report["scope"], "common_untouched": report["common_untouched_current_fit_samples"]}
    data = json.dumps(payload, ensure_ascii=True).replace("<", "\\u003c")
    warning = report["warning"] or "Current-fit holdout checks passed. Ancestral exposure and independent labels still need verification."
    html = _HTML.replace("REPORT_DATA", data).replace("REPORT_WARNING", escape(warning))
    path.write_text(html, encoding="utf-8")


_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SmartOMR Model and Ensemble Comparison</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#fff;color:#222a30;font:14px/1.5 Arial,sans-serif;letter-spacing:0}
main{max-width:1260px;margin:auto;padding:28px 24px}h1{font-size:27px;line-height:1.25;margin:0 0 10px}h2{font-size:20px;margin:0 0 16px}h3{font-size:16px;margin:16px 0 10px}
p{margin:8px 0 14px}header{border-bottom:2px solid #23715b;padding-bottom:16px}.muted,small{color:#52616b}
.warning{background:#fff2f3;border-left:4px solid #bc364a;padding:12px 16px;margin:20px 0}section{padding:24px 0;border-bottom:1px solid #dbe1e5}
.scroll{overflow-x:auto;max-width:100%}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:9px 11px;text-align:left;vertical-align:top;border-bottom:1px solid #dbe1e5}thead th{background:#edf2f4}tr.combined{background:#edf7f2;font-weight:bold}
.controls{display:flex;align-items:end;gap:16px;flex-wrap:wrap;margin-bottom:18px}label{display:block;font-size:13px;font-weight:bold}select{display:block;max-width:100%;font:14px Arial;padding:9px;background:#fff;border:1px solid #bdc8cf;border-radius:4px;margin-top:5px;min-height:38px}
fieldset{border:0;padding:0;margin:0;display:flex;gap:12px;flex-wrap:wrap}legend{font-size:13px;font-weight:bold;margin-bottom:7px}fieldset label{font-weight:normal}input{accent-color:#23715b}
.stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:16px}.stat{border-top:2px solid #bbc9cf;padding:10px 0}.stat small{display:block}.stat strong{display:block;font-size:23px;font-weight:bold}
.matrix{min-width:850px;table-layout:fixed}.matrix th:first-child{width:110px}.matrix td,.matrix th{text-align:center;padding:7px 3px}.matrix small{display:block;font-size:10px}.matrix .zero{color:#9aa3aa}
.detail-grid{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:24px}.error-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
article{border:1px solid #dbe1e5;border-radius:4px;padding:14px;min-width:0}article h3{margin-top:0;overflow-wrap:anywhere}article img{display:block;width:150px;max-width:100%;height:150px;object-fit:contain;background:white;border:1px solid #dbe1e5;margin-bottom:12px}
code{overflow-wrap:anywhere;font-size:12px}a{color:#14679c}.links{display:flex;flex-wrap:wrap;gap:14px}
@media(max-width:750px){main{padding:22px 16px}h1{font-size:23px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.detail-grid{grid-template-columns:minmax(0,1fr)}.error-grid{grid-template-columns:minmax(0,1fr)}.controls>label{width:100%}.controls select{width:100%}th,td{padding:8px}}
</style></head><body><main>
<header><h1>SmartOMR Model and Ensemble Comparison</h1><p id="intro" class="muted"></p></header>
<div class="warning"><b>Comparison validity</b><p>REPORT_WARNING</p><p id="untouched"></p></div>
<section><h2>All Members and Combined Results</h2><p class="muted">Digit classification uses the highest probability, without rejection. Accuracy is agreement with the recorded labels, not page ownership accuracy.</p><div id="overview"></div></section>
<section><h2>Model Detail</h2><div class="controls">
<label>Ensemble group<select id="group"></select></label><label>Model<select id="model"></select></label>
<fieldset><legend>Confusion matrix</legend><label><input type="radio" name="view" value="counts" checked>Counts</label><label><input type="radio" name="view" value="normalised">Row percentages</label><label><input type="radio" name="view" value="errors">Errors only</label></fieldset>
</div><div id="stats" class="stats"></div><p id="architecture" class="muted"></p>
<p class="muted">Rows: recorded label. Columns: prediction. Green indicates agreement; red indicates disagreement.</p><div id="matrix" class="scroll"></div>
<div class="detail-grid"><div><h3>Per-Class Performance</h3><div id="classes"></div></div><div><h3>Confidence and Coverage</h3><div id="selective"></div><p class="muted">Descriptive only: probability is not calibrated correctness. Do not select production thresholds using these results.</p><h3>Exposure Breakdown</h3><div id="exposure"></div></div></div>
</section><section><h2 id="error-heading">Failed Crops</h2><p class="muted">Raw crops with predictions and top probabilities. Review disputed labels against the source scan; model agreement alone is not ground truth.</p><div id="errors" class="error-grid"></div></section>
<section><h2>Method and Files</h2><p id="scope"></p><p>All members and ensembles see identical evaluation crops. Augmentation and dropout are disabled. Ensemble weights come from the saved validation accuracies, not these comparison results. The exposure audit covers recorded current fine-tuning splits, not every ancestor checkpoint. Recorded labels are not independently certified.</p>
<p>No UI checkpoint, label or production grouping rule was changed.</p><div class="links"><a href="model_comparison.csv">All member and ensemble metrics</a><a href="member_predictions.csv">Individual model predictions</a><a href="paired_predictions.csv">Ensemble predictions</a><a href="comparison_report.json">Full JSON report</a></div></section>
</main><script type="application/json" id="data">REPORT_DATA</script><script>
const data=JSON.parse(document.getElementById('data').textContent);
const $=id=>document.getElementById(id);
const pct=n=>n===null||n===undefined?'n/a':(n*100).toFixed(2)+'%';
function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
function table(headers,rows){const wrap=el('div',undefined,'scroll'),t=el('table'),head=el('thead'),tr=el('tr'),body=el('tbody');headers.forEach(h=>{const c=el('th',h);c.scope='col';tr.append(c);});head.append(tr);t.append(head);rows.forEach(r=>{const row=el('tr');r.forEach(v=>row.append(el('td',v)));body.append(row);});t.append(body);wrap.append(t);return wrap;}
const total=data.groups.reduce((n,g)=>n+g.models.length-1,0);
$('intro').textContent=`${data.groups.length} ensemble groups | ${total} individual models + ${data.groups.length} ensemble results | ${data.samples} identical crops`;
$('untouched').textContent=`Crops unused by all current fits: ${data.common_untouched}. Training and validation exposure are shown for every group.`;
$('scope').textContent=data.scope;
for(const g of data.groups){
 const section=el('div');section.append(el('h3',g.name));
 const a=g.exposure;section.append(el('p',`Current fit exposure: ${a.training_seen_samples} training, ${a.validation_seen_samples} validation, ${a.unused_current_fit_samples} unused.`, 'muted'));
 const rows=g.models.map(m=>{const s=m.metrics;return[m.model==='ensemble'?'Combined ensemble':`Member ${m.member}`,m.seed??'-',Array.isArray(m.dense_layers)?m.dense_layers.join('/'):m.dense_layers,`${s.correct} / ${s.test_samples}`,s.errors,pct(s.accuracy),s.macro_precision.toFixed(4),s.macro_recall.toFixed(4),s.macro_f1.toFixed(4),pct(m.common_training_unseen_subset.accuracy)];});
 const t=table(['Model','Seed','Dense layers','Correct','Errors','Accuracy','Macro precision','Macro recall','Macro F1','Not-trained subset*'],rows);t.querySelector('tbody tr:last-child').className='combined';section.append(t);$('overview').append(section);
}
$('overview').append(el('p','*Common subset not used for training; validation / checkpoint-selection exposure can remain. This is not necessarily an independent test.', 'muted'));
data.groups.forEach((g,i)=>{$('group').append(new Option(g.name,String(i)));});
function selected(){return data.groups[Number($('group').value)].models[Number($('model').value)];}
function changeGroup(){const g=data.groups[Number($('group').value)];$('model').replaceChildren();g.models.forEach((m,i)=>$('model').append(new Option(m.model==='ensemble'?'Combined ensemble':`Member ${m.member} (seed ${m.seed??'unknown'})`,String(i))));$('model').value=String(g.models.length-1);render();}
function render(){
 const m=selected(),s=m.metrics,view=document.querySelector('input[name="view"]:checked').value;
 $('stats').replaceChildren();[['Accuracy',pct(s.accuracy)],['Correct',`${s.correct} / ${s.test_samples}`],['Errors',s.errors],['Macro F1',s.macro_f1.toFixed(4)],['Weighted F1',s.weighted_f1.toFixed(4)]].forEach(([label,value])=>{const node=el('div',undefined,'stat');node.append(el('small',label),el('strong',String(value)));$('stats').append(node);});
 $('architecture').textContent=m.model==='ensemble'?`Weighted probability ensemble of ${m.member_count} models. Dense layers by member: ${m.dense_layers.join(', ')}.`:`Dense layers: ${m.dense_layers}; hidden widths: ${m.dense_hidden_sizes.join(', ')||'none'}; training dropout: ${m.dropout}; learning rate: ${m.learning_rate??'not recorded'}; optimiser: ${m.optimizer??'not recorded'}; ensemble weight: ${pct(m.ensemble_weight)}.`;
 const matrix=el('table',undefined,'matrix'),head=el('thead'),tr=el('tr'),body=el('tbody');tr.append(el('th','Actual / predicted'));s.labels.forEach(label=>{const c=el('th',label);c.scope='col';tr.append(c);});head.append(tr);matrix.append(head);
 s.confusion_matrix.forEach((row,i)=>{const r=el('tr'),support=row.reduce((a,b)=>a+b,0),h=el('th',s.labels[i]);h.scope='row';h.append(el('small',`n = ${support}`));r.append(h);row.forEach((count,j)=>{const c=el('td'),ratio=support?count/support:0;c.title=`Actual ${s.labels[i]}, predicted ${s.labels[j]}: ${count} of ${support} (${pct(ratio)})`;if(view==='errors'&&i===j){c.textContent='-';}else if(!count){c.append(el('span','0','zero'));}else{c.style.background=`rgba(${i===j?'27,111,88':'184,53,66'},${.12+.55*ratio})`;c.append(el('b',view==='normalised'?pct(ratio):String(count)),el('small',view==='normalised'?String(count)+' crops':pct(ratio)));}r.append(c);});body.append(r);});matrix.append(body);$('matrix').replaceChildren(matrix);
 $('classes').replaceChildren(table(['Class','Support','Precision','Recall','F1'],Object.entries(s.per_class).map(([label,c])=>[label,c.support,pct(c.precision),pct(c.recall),c.f1.toFixed(4)])));
 $('selective').replaceChildren(table(['Cutoff','Included','Coverage','Errors'],m.selective_diagnostics.map(d=>[d.threshold.toFixed(2),d.accepted,pct(d.coverage),d.accepted_errors])));
 $('exposure').replaceChildren(table(['Subset','Crops','Accuracy'],Object.entries(m.by_exposure).map(([label,c])=>[label,c.test_samples,pct(c.accuracy)])));
 $('error-heading').textContent=`Failed Crops: ${m.errors.length} (${m.model==='ensemble'?'combined ensemble':'member '+m.member})`;
 $('errors').replaceChildren();
 if(!m.errors.length)$('errors').append(el('p','No predictions differ from the recorded labels.'));
 m.errors.forEach(e=>{const card=el('article');card.append(el('h3',e.image_path.split(/[\\/]/).pop()));const image=el('img');image.src=data.crops[e.image_path];image.alt='Raw crop '+e.image_path;card.append(image,el('p',`Recorded label: ${e.expected} | Predicted: ${e.predicted} | Top probability: ${pct(e.confidence)}`),el('p',`Exposure: ${e.exposure}`,'muted'));
 const probs=Object.entries(e.probabilities).sort((a,b)=>b[1]-a[1]).slice(0,3);card.append(table(['Digit','Probability'],probs.map(([label,p])=>[label,pct(p)])),el('code',e.image_path));$('errors').append(card);});
}
$('group').addEventListener('change',changeGroup);$('model').addEventListener('change',render);document.querySelectorAll('input[name="view"]').forEach(input=>input.addEventListener('change',render));changeGroup();
</script></body></html>"""
