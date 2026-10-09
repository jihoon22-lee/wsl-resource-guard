"use strict";
// Route IDs survive notification clicks, authentication reloads and lost POST replies.
const investigationIds = { incident: "", target: "", action: "" };
let incidentStore = null, incidentDetail = null, targetDetail = null, operationDetail = null;
const incidentStates = { active: "지속 중", resolving: "회복 확인 중", resolved: "해소 확인", unknown: "현재 상태 미확인" };
const operationStates = { preview: "영향 확인", executing: "종료 요청 처리 중", verifying: "실제 종료 확인 중", remaining: "실행 중인 대상 있음", partial: "일부 요청 실패", completed: "확인한 대상 종료됨", unknown: "결과 미확인", rejected: "실행하지 않음", expired: "확인 기간 만료" };
const deliveryStates = { pending: "전송 대기", sending: "전송 시도 중", accepted: "전송 서버 접수", retry: "실패 · 재시도 대기", failed: "전송 실패", unknown: "접수 여부 미확인", skipped: "만료 또는 발송 조건 미충족" };
const targetKinds = { shared_runtime: "여러 작업의 공용 실행기", agent_process: "에이전트 실행 프로세스", tool: "MCP 도구 트리" };
const investigationPanel = (title, content) => `<div class="panel investigation-panel"><div class="panel-heading"><h2>${esc(title)}</h2></div><div class="panel-body">${content}</div></div>`;

function renderIncidentList() {
  const host = $("incident-list");
  if (!host) return;
  if (!incidentStore) { host.innerHTML = '<div class="panel empty">경보별 원인을 불러오는 중…</div>'; return; }
  const rows = [...incidentStore.incidents].sort((a,b) => Number(b.state !== "resolved")-Number(a.state !== "resolved") || b.opened_at-a.opened_at);
  host.innerHTML = investigationPanel("경보별 원인과 다음 행동", `${incidentStore.stale ? '<p class="row-error">최근 관측을 확인할 수 없습니다. 마지막 기록이며 회복을 뜻하지 않습니다.</p>' : ""}${incidentStore.truncated ? '<p class="row-error">표시 한도에 도달했습니다. 일부 기록이 생략됐습니다.</p>' : ""}<div class="incident-cards">${rows.length ? rows.map(r => `<a class="incident-card" href="#incident?id=${esc(r.id)}"><span class="pill ${r.state === "resolved" ? "good" : r.severity === "critical" ? "bad" : "warn"}">${esc(incidentStore.stale ? "최근 상태 미확인" : incidentStates[r.state] || r.state)}</span><strong>${esc(r.reason)}</strong><span>${esc(r.priority === "pressure" ? "실제 자원 압박 확인 필요" : "점유 상태와 작업 필요성 확인")}</span><span class="muted">발생 ${when(r.opened_at)} · 최근 관측 ${when(r.observed_at)}</span><span>원인과 영향 확인 →</span></a>`).join("") : '<p class="empty">기록된 경보가 없습니다. 관측 중단 여부는 마지막 수집 시각에서 확인하세요.</p>'}</div>`);
}

function targetCard(t) {
  return `<a class="incident-card" href="#target?id=${esc(t.id)}"><strong>${esc(t.provider)} · ${esc(targetKinds[t.kind] || "실행 대상")}</strong><span>${esc(t.project)} · RAM ${giB(t.rss_kib)} GiB · ${num(t.process_count)}개 프로세스</span><span>${esc((t.projects || []).map(p=>p.project).join(" · "))}</span><span>실제 작업과 중단 영향 확인 →</span></a>`;
}

function renderInvestigation() {
  if (currentView === "incident") renderIncidentDetail();
  if (currentView === "target") renderTargetDetail();
  if (currentView === "action") renderOperationDetail();
}
function renderIncidentDetail() {
  const host = $("incident-detail");
  if (!incidentDetail) { host.innerHTML = '<div class="panel empty">해당 경보를 확인하는 중…</div>'; return; }
  const r = incidentDetail.incident, e = r.evidence || {}, decision = r.decision || {};
  const readFailed = !!sectionStates.get("incidentDetail")?.error;
  const deliveries = Object.values(r.delivery?.destinations || {});
  const measure = r.measurement || {};
  host.innerHTML = `<a class="button secondary" href="#alerts">← 경보 목록</a>` + investigationPanel("무엇이 달라졌나요?", `<span class="pill ${r.state === "resolved" ? "good" : "warn"}">${esc(incidentDetail.stale ? "최근 상태 미확인" : incidentStates[r.state] || r.state)}</span><h3>${esc(r.reason)}</h3>${measure.value != null ? `<p><strong>${esc(measure.label)} ${esc(measure.value)} ${esc(measure.unit)}</strong> · 기준 ${esc(measure.threshold)} ${esc(measure.unit)}<br>경보 첫 관측 대비 ${Number(measure.change_since_open || 0) > 0 ? "+" : ""}${esc(measure.change_since_open ?? "—")} ${esc(measure.unit)}</p>` : ""}<p>${esc(r.guidance)}</p><p>발생 ${when(r.opened_at)}<br>최근 관측 ${when(r.observed_at)}${r.resolved_at ? "<br>해소 확인 " + when(r.resolved_at) : ""}</p>${incidentDetail.stale ? '<p class="row-error">최신 관측이 없습니다. 이 기록만으로 현재 상태나 종료 필요성을 판단하지 마세요.</p>' : ""}<p>관측 당시 가용 RAM ${esc(e.available_gib ?? "—")} GiB · PSI ${e.psi_some_avg60 == null ? "관측 불가" : Number(e.psi_some_avg60).toFixed(2)+"%"} · Swap-out ${esc(e.swap_out_mib_per_minute ?? "—")} MiB/분</p>`) +
    investigationPanel("어떤 작업이 관련돼 있나요?", `<p>주요 점유 대상입니다. 점유량과 문제의 인과관계는 다를 수 있습니다.</p><div class="incident-cards">${(r.targets || []).map(targetCard).join("") || '<p>특정 세션에 귀속되지 않은 자원 경보입니다. <a href="#disks">디스크</a> 또는 <a href="#top">전체 점유 현황</a>을 확인하세요.</p>'}</div>`) +
    investigationPanel("어떻게 대응할까요?", `<p>확인하거나 재알림을 미뤄도 경보가 해결된 것으로 처리하지 않습니다.</p>${decision.choice ? `<p>최근 대응: ${esc({acknowledge:"작업 확인함",defer:"나중에 재확인",resume:"재알림 재개"}[decision.choice] || decision.choice)} · ${when(decision.at)}${decision.until ? " · " + when(decision.until) + "까지" : ""}</p>` : ""}${r.state !== "resolved" && !incidentDetail.stale && !readFailed ? '<div class="action-grid"><button class="button secondary" data-incident-choice="acknowledge">작업 중 · 확인함</button><button class="button secondary" data-incident-choice="defer" data-minutes="30">30분 뒤 재확인</button><button class="button secondary" data-incident-choice="defer" data-minutes="60">1시간 뒤 재확인</button><button class="button secondary" data-incident-choice="resume">재알림 재개</button></div>' : '<p>조치 전 최신 관측과 현재 대상 정보를 확인하세요.</p>'}<p id="incident-feedback" role="status"></p>`) +
    investigationPanel("알림 전달 상태", `<p>전송 서버 접수와 휴대전화·이메일의 실제 수신은 별개입니다.</p>${incidentDetail.delivery_unavailable ? '<p class="row-error">전달 기록을 읽지 못했습니다. 전송 성공을 확인할 수 없습니다.</p>' : ""}${deliveries.map(d=>`<p><strong>${esc(d.label)}</strong> · ${esc(deliveryStates[d.status] || d.status)}<br><span class="muted">${esc(d.detail || "")} · 시도 ${num(d.attempts)}회 · ${when(d.attempted_at)}</span></p>`).join("") || '<p>아직 전송 기록이 없습니다. 설정·등록 상태를 확인하세요.</p>'}`);
  host.querySelectorAll('[data-incident-choice]').forEach(b=>b.addEventListener('click', async()=>{
    const id = r.id;
    b.disabled = true;
    try {
      const result = await api(`/api/incidents/${id}/decision`, {choice:b.dataset.incidentChoice,minutes:Number(b.dataset.minutes || 0)});
      if (currentView === "incident" && investigationIds.incident === id) { $("incident-feedback").textContent=result.message; await refresh(true); }
    } catch(error) { if ($("incident-feedback")) $("incident-feedback").textContent=error.message; }
    finally { b.disabled=false; }
  }));
}

function targetDescription(t) {
  return `<span class="pill ${t.kind === "shared_runtime" ? "warn" : ""}">${esc(targetKinds[t.kind] || t.kind)}</span><h3>${esc(t.provider)} · ${esc(t.name)}</h3><p>${esc(t.identity_note)}</p><ul>${(t.tasks || []).map(task=>`<li>${esc(task.title || "제목 미제공")}<span class="subline">명시적 작업 ID ${esc(task.thread_id)}</span></li>`).join("")}</ul><p>실행 위치: <span class="path-text">${esc(t.cwd || "관측 불가")}</span><br>시작 약 ${when((t.observed_at || Date.now()/1000)-t.age_seconds)} · PID ${num(t.root_pid)}</p><p>연결된 프로젝트: ${esc((t.projects || []).map(p=>p.project).join(" · ") || "확인 불가")}</p>${t.projects_complete === false || t.tasks_complete === false ? '<p class="row-error">일부 항목만 표시됩니다.</p>' : ""}<p>RAM ${giB(t.rss_kib)} GiB · Swap ${giB(t.swap_kib)} GiB<br>프로세스 ${num(t.process_count)}개 · MCP 도구 트리 ${num(t.mcp_tree_count)}개</p><p class="muted">${esc(t.memory_note)}</p><p><strong>${esc(t.impact)}</strong></p><p>${esc(t.recovery_note)}</p>`;
}
function renderTargetDetail() {
  const host = $("target-detail");
  if (!targetDetail) { host.innerHTML='<div class="panel empty">현재 대상과 영향 범위를 확인하는 중…</div>'; return; }
  const toolQuery = $("tool-filter")?.value || "";
  const t = targetDetail;
  const targetReadFailed = !!sectionStates.get("targetDetail")?.error;
  host.innerHTML = '<a class="button secondary" href="#alerts">경보 목록</a>' + investigationPanel("실제 작업과 공유 범위", targetDescription(t)) + investigationPanel("다음 행동", `${t.killable && !targetReadFailed ? '<p>원래 앱에서 작업 중지·저장을 먼저 확인하세요. 이 화면에서는 아래 대상의 종료 요청만 가능합니다.</p><button id="preview-termination" class="button danger">종료 영향 미리보기</button>' : `<p class="row-error">${esc(t.kill_block_reason)}</p><p>원래 ${esc(t.provider)} 앱에서 위 프로젝트와 작업 ID를 확인하세요. 개별 도구도 사용 중인 작업을 확인한 뒤 판단하세요.</p>`}<p id="target-feedback" role="status"></p>`) + investigationPanel("연결된 도구", `<p>도구 종료는 연결된 작업의 호출을 중단시킬 수 있습니다. 아래 항목에서 소유 관계와 범위를 확인하세요.</p><label for="tool-filter">프로젝트·도구 이름으로 찾기</label><input id="tool-filter" class="search" type="search" value="${esc(toolQuery)}" placeholder="예: 프로젝트 이름 또는 Playwright"><p class="muted">메모리 점유 순 · 관측한 도구 ${num(t.tools_total ?? t.tools?.length ?? 0)}개${(t.tools_total || 0) > 64 ? " · 상위 64개 표시" : ""}</p><div id="target-tools" class="incident-cards"></div>`);
  const renderTools=()=>{
    const term=$("tool-filter").value.toLowerCase();
    const tools=(t.tools || []).filter(tool=>[tool.name,tool.project,String(tool.root_pid || "")].join(" ").toLowerCase().includes(term));
    $("target-tools").innerHTML=tools.map(tool=>`<a class="incident-card" href="#target?id=${esc(tool.id)}"><strong>${esc(tool.name)}</strong><span>${esc(tool.project)} · ${giB(tool.rss_kib)} GiB · ${num(tool.process_count)}개 프로세스</span>${tool.age_seconds != null ? `<span>실행 ${age(tool.age_seconds)} · 진행 여부는 원래 앱에서 확인</span>` : ""}<span>도구 영향 확인 →</span></a>`).join("") || '<p>조건에 맞는 독립 도구가 없습니다.</p>';
  };
  $("tool-filter").addEventListener('input',renderTools);renderTools();
  $("preview-termination")?.addEventListener('click',async()=>{
    const id=t.id, b=$("preview-termination");b.disabled=true;
    try {
      const result=await api('/api/actions/preview',{target_id:id});
      if (currentView!=="target" || investigationIds.target!==id) return;
      operationDetail=result;
      sessionStorage.setItem('wrg-action',result.id);
      navigate('action',false,{id:result.id});
    } catch(error) { if ($("target-feedback")) $("target-feedback").textContent=error.message; }
    finally { b.disabled=false; }
  });
}
function renderOperationDetail() {
  const host=$("operation-detail");
  if (!operationDetail) { host.innerHTML='<div class="panel empty">저장된 조치 결과를 확인하는 중…</div>'; return; }
  const r=operationDetail, t=r.target;
  const checked=host.querySelector('#understand-impact')?.checked;
  const preview=r.status==='preview' && r.expires_at>Date.now()/1000 && !sectionStates.get("operationDetail")?.error;
  host.innerHTML=investigationPanel(operationStates[r.status] || r.status, `<p role="status"><strong>${esc(r.message)}</strong></p>${targetDescription(t)}${r.signalled?.length ? `<p>종료 신호 요청: ${num(r.signalled.length)}개</p>` : ""}${r.remaining?.length ? `<p class="row-error">아직 실행 중인 PID: ${r.remaining.map(num).join(', ')}</p>` : ""}${r.replacement_observed ? '<p class="row-error">같은 PID에 새 프로세스가 관측됐습니다. 기존 확인으로 조치하지 않습니다.</p>' : ""}${preview ? `<p>확인 유효 시각 ${when(r.expires_at)}. 영향 범위가 변경되면 실행하지 않습니다.</p><label class="impact-consent"><input type="checkbox" id="understand-impact" ${checked ? 'checked' : ''}> 위 중단 영향을 이해했고 이 대상의 종료를 요청합니다.</label><div class="action-grid"><button id="execute-termination" class="button danger" ${checked ? '' : 'disabled'}>확인한 대상 종료 요청</button><a class="button secondary" href="#target?id=${esc(t.id)}">취소 · 작업으로 돌아가기</a></div>` : `<button id="check-operation" class="button secondary">저장된 결과 다시 확인</button><a class="button secondary" href="#alerts">경보 해소 여부 확인</a>`}<p id="operation-feedback" role="status"></p>`);
  $("understand-impact")?.addEventListener('change',e=>{ $("execute-termination").disabled=!e.target.checked; });
  $("execute-termination")?.addEventListener('click',async()=>{
    const id=r.id, b=$("execute-termination");b.disabled=true;
    try {
      const result=await api(`/api/actions/${id}/execute`,{confirmed:true});
      if (currentView==='action' && investigationIds.action===id) { operationDetail=result;renderOperationDetail(); }
    } catch(error) {
      if (currentView==='action' && investigationIds.action===id) {
        operationDetail={...r,status:'unknown',message:'요청의 처리 여부를 확인하지 못했습니다. 종료를 다시 보내지 않고 저장된 결과를 조회하세요.'};
        renderOperationDetail();
        $("operation-feedback").textContent=error.message;
      }
    }
    setTimeout(()=>{ if(currentView==='action' && investigationIds.action===id) refresh(true); },2000);
  });
  $("check-operation")?.addEventListener('click',()=>refresh(true));
}
