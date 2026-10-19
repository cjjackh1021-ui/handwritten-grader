"""Synthetic export/UI fixture only; NOT an OCR accuracy dataset.

python scripts/handwriting_demo.py [--serve]  (preview at localhost:5056)
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ["JOB_TTL_DAYS"]="0"
from PIL import Image,ImageDraw,ImageFont
from graders import config,extract,review
config.OUTPUT_DIR=ROOT/"output"/"_handwriting_demo"
config.WORK_DIR=config.OUTPUT_DIR/"_work"
import app as web
job_id="synthetic-demo";directory=config.OUTPUT_DIR/job_id;directory.mkdir(parents=True,exist_ok=True)
path=directory/"synthetic-student.png"
image=Image.new("RGB",(1240,1754),"white");draw=ImageDraw.Draw(image)
printed=ImageFont.truetype("C:/Windows/Fonts/arial.ttf",34)
student=ImageFont.truetype(str(ROOT/"assets/handwriting/李国夫手写体.ttf"),65)
draw.text((80,65),"SYNTHETIC RENDERING TEST - NOT HUMAN EVALUATION DATA",font=ImageFont.truetype("C:/Windows/Fonts/arial.ttf",23),fill="black")
questions=[]
for index,(answer,score,full,pending) in enumerate([("2 + 3 = 5",3,3,False),("x + 2 = 5, x = 2",1.5,3,False),("4 x 2 = 6",0,3,False),("?",0,3,True)],1):
    y=190+(index-1)*340
    draw.text((80,y),f"{index}. Show your answer:",font=printed,fill="black")
    draw.text((110,y+70),answer,font=student,fill=(25,35,65))
    questions.append(dict(id=f"q{index}",no=str(index),type="math",student_answer=answer,answer_state="unreadable" if pending else "present",
                         score=score,full=full,recognition_review=pending,position_review=False,grading_review=False,pages=[1],
                         regions=[{"page":1,"role":"answer","bbox":[.07,(y+65)/1754,.68,(y+165)/1754]}],
                         review_reasons=["字迹无法确认，请复核"] if pending else []))
image.save(path)
report=extract.inspect_image(path,directory/"_work",False)
item=web.build_entry(report,dict(score=4.5,full_score=12,task_type="exam",comment="部分题目待复核",details={"questions":questions}),"success","")
review.aggregate(item)
final=web.finalize_job(job_id,directory,[item],"",dict(task_type="auto",ocr_mode="off"),[])
fixture=dict(job_id=job_id,results=[item],**final)
(directory/"ui-fixture.json").write_text(json.dumps(fixture,ensure_ascii=False,indent=2),encoding="utf-8")
print(item.get("marked_pdf_path") or item.get("stamp_error"),flush=True)
if "--serve" in sys.argv:
    from flask import jsonify
    @web.app.get("/demo-fixture")
    def get_fixture():return jsonify(fixture)
    web.app.run(host="127.0.0.1",port=5056,debug=False)
