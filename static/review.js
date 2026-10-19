/* Original pixels + normalized page geometry; server validates every edit. */
(() => {
    const dialog = document.createElement('dialog');
    dialog.id = 'questionReview';
    dialog.style.cssText = 'margin:auto;width:min(1250px,96vw);max-height:94vh;border:0;border-radius:12px;padding:20px;overflow:auto';
    dialog.innerHTML = `<div style="display:flex;justify-content:space-between"><h2>逐题复核</h2><button id="rvClose">关闭</button></div>
      <p id="rvStatus" role="status"></p>
      <div style="display:flex;gap:16px;flex-wrap:wrap">
        <section style="flex:2;min-width:300px">
          <label>页面 <select id="rvPage"></select></label>
          <label><input type="checkbox" id="rvMarked">显示批改结果</label>
          <select id="rvTool" aria-label="调整工具"><option value="view">查看</option><option value="answer">框选本页作答区域</option><option value="question">框选本页题干区域</option><option value="mark">框选标记空白位置</option><option value="move">拖动已有标记</option></select>
          <p style="font-size:12px">在原图拖动可调整区域；跨页题逐页框选。框选替换本页同类区域，其他页保留。蓝框为作答，紫框为题干，红框为标记。</p>
          <button id="rvSaveLocation">保存位置</button>
          <div style="max-height:65vh;overflow:auto"><canvas id="rvCanvas" style="width:100%;touch-action:none;border:1px solid #ddd"></canvas></div>
        </section>
        <section style="flex:1;min-width:280px">
          <label>小题 <select id="rvQuestion"></select></label><p id="rvReasons"></p>
          <img id="rvCrop" alt="当前小题原图裁片" style="max-width:100%;max-height:160px;object-fit:contain" hidden>
          <details open><summary>识别结果对照</summary><div id="rvSources"></div></details>
          <label for="rvAnswer">确认后的学生原文（保留学生写错的内容）</label><textarea id="rvAnswer" rows="6" style="width:100%"></textarea>
          <select id="rvAnswerState" aria-label="作答状态"><option value="present">有作答</option><option value="blank">已确认空白</option><option value="unreadable">无法识别，继续待复核</option></select>
          <button id="rvRegrade">确认文字并仅重批此题</button>
          <hr><label for="rvScore">人工分数</label><input id="rvScore" type="number" min="0" step="any"><span id="rvFull"></span>
          <label><input type="checkbox" id="rvConfirm">同时确认现有转写无误</label><button id="rvSaveScore">保存人工分数</button>
          <p style="font-size:12px">未解决的识别或评分问题显示暂计总分；草稿仍可导出。</p>
        </section>
      </div>`;
    document.body.append(dialog);
    const css=document.createElement('style');css.textContent='#questionReview::backdrop{background:rgba(20,30,55,.5)}#questionReview button{border:1px solid #ccd2e1;border-radius:6px;padding:8px 12px;background:#f3f5ff;cursor:pointer;margin:6px 0}#questionReview label{display:block;margin:8px 0 4px}#questionReview p{margin:8px 0;line-height:1.5}#questionReview #rvStatus{padding:10px;background:#f3f5ff;border-radius:6px}#questionReview section{overflow:hidden}';document.head.append(css);
    const el = id => document.getElementById(id);
    const canvas = el('rvCanvas'), ctx = canvas.getContext('2d');
    let item, question, image, regions = [], placement, start, dragOriginal, sequence = 0, busy = false;
    const pageNo = () => Number(el('rvPage').value);
    const base = () => `/preview/${encodeURIComponent(currentJob)}/${encodeURIComponent(item.report_id)}/${pageNo()}`;
    function draw() {
        if (!image) return;
        canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
        ctx.drawImage(image, 0, 0);
        if (el('rvMarked').checked) return;
        ctx.lineWidth = Math.max(2, canvas.width / 450);
        function box(b, color) { ctx.strokeStyle = color; ctx.strokeRect(b[0]*canvas.width,b[1]*canvas.height,(b[2]-b[0])*canvas.width,(b[3]-b[1])*canvas.height); }
        regions.filter(r => r.page === pageNo()).forEach(r => box(r.bbox, r.role === 'answer' ? '#2563eb' : '#8b5cf6'));
        if (placement?.page === pageNo()) box(placement.bbox, '#dc2626');
    }
    async function loadPage() {
        const seq = ++sequence;
        image = null; ctx.clearRect(0,0,canvas.width,canvas.height);
        const img = new Image();
        img.onload = () => { if (seq !== sequence) return; image = img; draw(); };
        img.onerror = () => { if (seq === sequence) el('rvStatus').textContent = '此页预览不可用，请查看导出错误。'; };
        img.src = base() + '?marked=' + el('rvMarked').checked + '&v=' + Date.now();
        sources();
    }
    function sources() {
        const page = item.pages.find(p => p.page_num === pageNo());
        el('rvSources').replaceChildren();
        (page?.ocr?.results || []).forEach(result => {
            const details = document.createElement('details'); details.open = true;
            const title = document.createElement('summary'); title.textContent = result.engine;
            const text = document.createElement('pre'); text.style.cssText = 'white-space:pre-wrap;max-height:180px;overflow:auto';
            text.textContent = result.text || '未识别出文字'; details.append(title,text); el('rvSources').append(details);
        });
        (page?.ocr?.issues || []).forEach(issue => { const p = document.createElement('p'); p.textContent = issue.message; el('rvSources').append(p); });
        const crop = el('rvCrop');
        crop.hidden = !question || !regions.some(r => r.page === pageNo());
        if (!crop.hidden) { crop.src = base() + '?question=' + encodeURIComponent(question.id) + '&v=' + Date.now(); crop.onerror = () => crop.hidden = true; }
    }
    function selectQuestion() {
        question = (item.details?.questions || []).find(q => q.id === el('rvQuestion').value);
        regions = structuredClone(question?.regions || []); placement = structuredClone(question?.placement || null);
        el('rvAnswer').value = question?.student_answer || '';
        el('rvAnswerState').value = question?.answer_state || 'present';
        el('rvScore').value = question?.score ?? ''; el('rvScore').max = question?.full ?? 100;
        el('rvFull').textContent = ' / ' + (question?.full ?? ''); el('rvConfirm').checked = false;
        el('rvReasons').textContent = (question?.review_reasons || []).join('；') || (question ? '此题已确认，可继续修正' : '此任务没有逐题数据');
        for (const id of ['rvRegrade','rvSaveScore','rvSaveLocation']) el(id).disabled = !question;
        const page = question?.pages?.[0] || regions[0]?.page;
        if (page) el('rvPage').value = page;
        loadPage();
    }
    function showItem(value, selectedId) {
        item = value;
        el('rvStatus').textContent = `${item.student_name} · ${item.score_provisional ? '暂计' : '得分'} ${item.score}/${item.full_score} · ${item.review_required ? '草稿' : '已确认'}` + (item.stamp_error ? ' · 导出未完成：'+item.stamp_error : '');
        el('rvPage').replaceChildren(...item.pages.map(p => new Option('第'+p.page_num+'页',p.page_num)));
        if (el('rvMarked').checked) {
            for (let n=item.pages.length+1; n<=(item.marked_pngs || []).length;n++) el('rvPage').add(new Option('批改汇总 '+n,n));
        }
        el('rvQuestion').replaceChildren(...(item.details?.questions || []).map(q => new Option(`${q.no} · ${q.score}/${q.full} · ${q.review_required ? '待复核' : '已确认'}`,q.id)));
        if (selectedId) el('rvQuestion').value = selectedId;
        selectQuestion();
    }
    document.getElementById('resultsBody').addEventListener('click', event => {
        const button = event.target.closest('.review-question-btn');
        if (!button) return;
        el('rvMarked').checked = false;
        showItem(lastResults[Number(button.dataset.idx)]); dialog.showModal();
    });
    el('rvClose').onclick = () => { if (!busy) dialog.close(); };
    dialog.addEventListener('cancel', e => { if (busy) e.preventDefault(); });
    el('rvQuestion').onchange = selectQuestion; el('rvPage').onchange = loadPage;
    el('rvMarked').onchange = () => showItem(item, question?.id);
    function point(e) { const rect = canvas.getBoundingClientRect(); return [Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),Math.max(0,Math.min(1,(e.clientY-rect.top)/rect.height))]; }
    canvas.onpointerdown = e => {
        if (!question || busy || el('rvMarked').checked || el('rvTool').value === 'view') return;
        start = point(e); dragOriginal = placement ? structuredClone(placement) : null; canvas.setPointerCapture(e.pointerId);
    };
    canvas.onpointermove = e => {
        if (!start) return;
        const end = point(e), tool = el('rvTool').value;
        let box = [Math.min(start[0],end[0]),Math.min(start[1],end[1]),Math.max(start[0],end[0]),Math.max(start[1],end[1])];
        if (tool === 'move' && dragOriginal?.page === pageNo()) {
            const b = dragOriginal.bbox, w=b[2]-b[0],h=b[3]-b[1],x=Math.max(0,Math.min(1-w,b[0]+end[0]-start[0])),y=Math.max(0,Math.min(1-h,b[1]+end[1]-start[1]));
            box=[x,y,x+w,y+h]; placement={page:pageNo(),bbox:box,manual:true};
        } else if (tool === 'mark') placement={page:pageNo(),bbox:box,manual:true};
        else if (tool === 'answer' || tool === 'question') regions=[...regions.filter(r => !(r.page === pageNo() && r.role === tool)),{page:pageNo(),bbox:box,role:tool}];
        draw();
    };
    canvas.onpointerup = canvas.onpointercancel = () => { start=null; };
    async function save(data) {
        if (!question || busy) return;
        busy=true; el('rvStatus').textContent='正在保存并重生成预览…';
        const rid=item.report_id,qid=question.id;
        try {
            const response=await fetch(`/review/${encodeURIComponent(currentJob)}/${rid}/${qid}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
            const result=await response.json(); if (!response.ok) throw new Error(result.error || '保存失败');
            renderResults(result.results,result.statistics,result.artifacts,[]);
            showItem(result.results.find(r => r.report_id === rid),qid);
        } catch (error) { el('rvStatus').textContent=error.message; }
        finally { busy=false; }
    }
    el('rvSaveLocation').onclick = () => save({action:'location',regions,...(placement ? {placement} : {})});
    el('rvSaveScore').onclick = () => save({action:'score',score:el('rvScore').value,confirm_answer:el('rvConfirm').checked});
    el('rvRegrade').onclick = () => save({...Object.fromEntries(baseForm().entries()),action:'transcript',student_answer:el('rvAnswer').value,answer_state:el('rvAnswerState').value});
})();
