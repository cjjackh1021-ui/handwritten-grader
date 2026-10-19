"""Evaluate human-labelled pages; never infer ground truth from OCR or answers.

python -m eval.handwriting_eval --labels path/to/labels.jsonl --out eval/handwriting-results
Optional --provider/--model enable the same segmentation/visual adjudication as the app.
API keys are read from the provider's environment variable, never recorded.
"""
import argparse
import json
from pathlib import Path
import statistics
import time
from graders import annotations, autodetect, config, extract, providers


def normalized(text):
    return "".join(str(text).split())


def edit_distance(a, b):
    previous=list(range(len(b)+1))
    for i,char in enumerate(a,1):
        row=[i]
        for j,other in enumerate(b,1):row.append(min(row[-1]+1,previous[j]+1,previous[j-1]+(char!=other)))
        previous=row
    return previous[-1]


def iou(a,b):
    overlap=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    area=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-overlap
    return overlap/area if area else 0


def score_page(label,prediction):
    expected=normalized(label["text"]); actual=normalized(prediction.get("text",""))
    questions=label.get("questions",[]); predicted={str(q["no"]):q for q in prediction.get("questions",[])}
    hits=[]
    for q in questions:
        found=predicted.get(str(q["no"]),{})
        boxes=[r["bbox"] for r in found.get("regions",[]) if r.get("role")=="answer"]
        hits.append(iou(q["answer_bbox"],annotations.union(boxes))>=.5 if boxes else False)
    formula_hits=[]
    for formula in label.get("formulas",[]):
        blocks=[b for b in prediction.get("blocks",[]) if iou(formula["bbox"],b["bbox"])>.1]
        # Exact normalized transcription, not symbolic equivalence: student errors must remain.
        if "formula_predictions" in prediction:
            index=len(formula_hits)
            predicted=prediction["formula_predictions"][index] if index < len(prediction["formula_predictions"]) else ""
            formula_hits.append(normalized(predicted)==normalized(formula["text"]))
        else:
            formula_hits.append(any(normalized(b["text"])==normalized(formula["text"]) for b in blocks))
    return dict(char_errors=edit_distance(expected,actual),char_count=len(expected),
                formula_correct=sum(formula_hits),formula_count=len(formula_hits),
                location_correct=sum(hits),question_count=len(hits))


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--labels",type=Path,required=True);p.add_argument("--out",type=Path,default=Path("eval/handwriting-results"))
    p.add_argument("--provider",default="");p.add_argument("--model",default="");p.add_argument("--base-url",default="")
    p.add_argument("--predictions",type=Path,help="Replay saved JSONL: mode, page_id, text, blocks, questions, seconds, complete")
    args=p.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    labels=[json.loads(line) for line in args.labels.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not labels or any(not l.get("human_verified") or not isinstance(l.get("text"),str) for l in labels):
        raise ValueError("每页须包含人工核验原文及 human_verified=true")
    if len({l["page_id"] for l in labels})!=len(labels):raise ValueError("page_id 须唯一")
    for label in labels:
        for q in label.get("questions",[]):
            if not annotations.valid_box(q.get("answer_bbox")):raise ValueError("人工作答框必须使用规范化页面上的 0..1 坐标")
    choice=None
    if args.provider:
        import os
        key=os.getenv(config.PROVIDERS.get(args.provider,{}).get("env_key","CUSTOM_API_KEY"),"")
        if not key:raise ValueError("请先配置所选服务商的 API Key 环境变量")
        choice=providers.ModelChoice(provider=args.provider,api_key=key,model=args.model,base_url=args.base_url,use_vision=True)
    replay={}
    if args.predictions:
        replay={(r["mode"],r["page_id"]):r for r in map(json.loads,args.predictions.read_text(encoding="utf-8").splitlines())}
    results={};records=[]
    for mode in ("tesseract","paddle","hybrid"):
        measurements=[]
        for label in labels:
            started=time.monotonic()
            if args.predictions:
                prediction=replay.get((mode,label["page_id"]),dict(text="",complete=False,error="prediction missing"))
            else:
                try:
                    path=(args.labels.parent/label["image"]).resolve()
                    report=extract.inspect_image(path,args.out/"work"/mode,ocr_mode=mode)
                    data=report.pages[0].ocr
                    prediction=dict(text=data.get("text",""),blocks=data.get("blocks",[]),complete=data.get("enhancement_complete",False),issues=data.get("issues",[]))
                    if choice:
                        segmented=autodetect.segment(choice,report,[])
                        prediction["questions"]=segmented["questions"]
                        prediction["visual_adjudication"]=True
                    prediction["seconds"]=time.monotonic()-started
                    prediction["cached"]=any(r.get("cached") for r in data.get("results",[]))
                    from PIL import Image
                    from graders import ocr
                    formula_started=time.monotonic()
                    prediction["formula_predictions"]=[]
                    with Image.open(report.pages[0].render_path) as image:
                        for index,formula in enumerate(label.get("formulas",[])):
                            if not annotations.valid_box(formula.get("bbox")):raise ValueError("公式标注框无效")
                            box=formula["bbox"]
                            crop=args.out/"work"/mode/f"formula-{annotations.stable_id(label['page_id'])}-{index}.png"
                            image.crop(tuple(int(v*(image.width if i%2==0 else image.height)) for i,v in enumerate(box))).save(crop)
                            recognized=ocr.recognize(crop,args.out/"work"/mode,mode)
                            value=next((r["text"] for r in recognized.get("results",[]) if r["engine"]=="chandra"),recognized.get("text",""))
                            prediction["formula_predictions"].append(value)
                            prediction["complete"] &= recognized.get("enhancement_complete",False)
                    prediction["formula_crop_seconds"]=time.monotonic()-formula_started
                except Exception as exc:
                    prediction=dict(text="",complete=False,error=str(exc),seconds=time.monotonic()-started)
            measurement=score_page(label,prediction);measurement.update(seconds=prediction.get("seconds"),complete=prediction.get("complete",False),layout_available="questions" in prediction)
            measurements.append(measurement)
            records.append({"mode":mode,"page_id":label["page_id"],**prediction})
            print(f"{mode} {label['page_id']}: {'completed' if measurement['complete'] else 'incomplete'}",flush=True)
        def ratio(a,b):
            denominator=sum(m[b] for m in measurements)
            return sum(m[a] for m in measurements)/denominator if denominator else None
        times=[m["seconds"] for m in measurements if m["seconds"] is not None]
        results[mode]={"character_error_rate":ratio("char_errors","char_count"),"formula_exact_accuracy":ratio("formula_correct","formula_count"),
                       "location_accuracy_iou_0_5":ratio("location_correct","question_count") if all(m["layout_available"] for m in measurements) else None,
                       "mean_seconds_per_page":statistics.mean(times) if times else None,"complete_pages":sum(m["complete"] for m in measurements),"pages":len(labels)}
    enough=len(labels)>=30 and sum(len(l.get("questions",[])) for l in labels)>=200
    full=all(r["complete_pages"]==len(labels) and r["location_accuracy_iou_0_5"] is not None and r["formula_exact_accuracy"] is not None for r in results.values())
    report={"human_labelled_pages":len(labels),"human_labelled_questions":sum(len(l.get("questions",[])) for l in labels),
            "acceptance_complete":enough and full,"results":results,
            "notes":["无人工标注数据时不能声称准确率提升。", "位置指标须运行拆题或提供预测文件；否则为 null。", "CER 比较整页 OCR 原文；双引擎原文保留 Paddle 主转写。", "公式在相同人工区域裁图上单独识别，双引擎优先 Chandra；严格转写匹配，不等同于数学语义正确率。公式裁图耗时单独记录。", "缓存状态保留在 predictions.jsonl；首次运行用于冷启动耗时对比。"]}
    (args.out/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    (args.out/"predictions.jsonl").write_text("\n".join(json.dumps(r,ensure_ascii=False) for r in records),encoding="utf-8")
    lines=["# 手写识别人工样本评测",f"验收状态：{'数据与指标齐全' if report['acceptance_complete'] else '未完成'}",f"人工样本：{len(labels)} 页，{report['human_labelled_questions']} 小题。"]
    for mode,metrics in results.items():
        lines.append(f"\n## {mode}")
        lines.extend(f"- {key}: {value if value is not None else '未测量'}" for key,value in metrics.items())
    lines.extend(report["notes"])
    (args.out/"report.md").write_text("\n".join(lines),encoding="utf-8")


if __name__ == "__main__":main()
