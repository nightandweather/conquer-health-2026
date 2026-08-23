const pptxgen = require("pptxgenjs");
const p = new pptxgen();
p.layout = "LAYOUT_WIDE";                 // 13.3 x 7.5
const W = 13.3, H = 7.5;

const INK="0B3C49", TEAL="127A8C", MINT="06D6A0", ROSE="EF476F",
      GREY="5B6B72", LIGHT="F1F5F6", WHITE="FFFFFF";
const KF = "Malgun Gothic";               // 한글
const MF = "Courier New";

const dark = () => { const s=p.addSlide(); s.background={color:INK}; return s; };
const light = () => { const s=p.addSlide(); s.background={color:WHITE}; return s; };

function title(s, t, sub, onDark){
  s.addText(t, {x:0.7,y:0.5,w:W-1.4,h:0.75,fontFace:KF,fontSize:34,bold:true,
    color:onDark?WHITE:INK, margin:0});
  if(sub) s.addText(sub, {x:0.7,y:1.25,w:W-1.4,h:0.4,fontFace:KF,fontSize:14,
    color:onDark?"9FC2CB":GREY, margin:0});
}
function card(s,x,y,w,h,fill){ s.addShape(p.ShapeType.roundRect,{x,y,w,h,
  fill:{color:fill||LIGHT}, line:{color:fill||LIGHT}, rectRadius:0.08}); }
function stat(s,x,y,w,num,label,col){
  s.addText(num,{x,y,w,h:0.85,fontFace:"Arial",fontSize:44,bold:true,color:col||INK,align:"center",margin:0});
  s.addText(label,{x,y:y+0.85,w,h:0.4,fontFace:KF,fontSize:12,color:GREY,align:"center",margin:0});
}

/* 1 ─ 표지 */
{ const s=dark();
  s.addShape(p.ShapeType.roundRect,{x:0.7,y:1.15,w:2.5,h:0.42,fill:{color:MINT},line:{color:MINT},rectRadius:0.2});
  s.addText("벤치마크 상 · 1위",{x:0.7,y:1.15,w:2.5,h:0.42,fontFace:KF,fontSize:13,bold:true,color:INK,align:"center",margin:0});
  s.addText("HealthBench 52.88",{x:0.7,y:1.85,w:W-1.4,h:1.2,fontFace:"Arial",fontSize:60,bold:true,color:WHITE,margin:0});
  s.addText("의과학 특화 파운데이션 모델을 20.4점 끌어올린 방법",
    {x:0.7,y:3.05,w:W-1.4,h:0.5,fontFace:KF,fontSize:20,color:"9FC2CB",margin:0});
  s.addText("Conquer Health 해커톤 · 2026.08.21–22 · 팀 AIM",
    {x:0.7,y:6.35,w:8,h:0.35,fontFace:KF,fontSize:13,color:"7FA6B0",margin:0});
  s.addText("박주희 · 박지우 · 안균승 · 이장원 · 이강훈",
    {x:0.7,y:6.72,w:8,h:0.35,fontFace:KF,fontSize:12,color:"5E8996",margin:0});
  s.addNotes("Lunit L2(30B/A5B MoE) 기반 대국민 건강상담 챗봇. HealthBench Consensus 자동채점에서 참가 18팀 중 1위.");
}

/* 2 ─ 결과 */
{ const s=light();
  title(s,"결과","32.44 → 52.88 · 참가 18팀 중 1위 · 2위와 2.61점 차");
  const chart=[{name:"HealthBench 점수",
    labels:["Trial 7","Trial 8","Trial 16","Trial 19","Trial 22","Trial 25","Trial 43","Trial 49"],
    values:[23.57,29.13,32.44,39.26,43.07,49.72,51.16,52.88]}];
  s.addChart(p.ChartType.line,chart,{x:0.7,y:1.9,w:8.1,h:4.7,
    showTitle:false, showLegend:false, lineDataSymbol:"circle", lineDataSymbolSize:7,
    chartColors:[TEAL], lineSize:3, showValue:true, dataLabelFontFace:"Arial",
    dataLabelFontSize:10, dataLabelColor:INK, dataLabelPosition:"t",
    catAxisLabelColor:GREY, valAxisLabelColor:GREY, catAxisLabelFontSize:10,
    valAxisLabelFontSize:10, valAxisMinVal:20, valAxisMaxVal:56,
    valGridLine:{color:"E3EAEC",size:1}, catGridLine:{style:"none"},
    catAxisLabelFontFace:"Arial", valAxisLabelFontFace:"Arial"});
  card(s,9.1,1.9,3.5,4.7);
  const rows=[["1","AIM","52.88"],["2","창억떡","50.27"],["3","daintlab-C","49.77"],
              ["4","만능의아스피린","49.30"],["5","Tricare","48.88"]];
  s.addText("최종 리더보드",{x:9.4,y:2.1,w:3,h:0.35,fontFace:KF,fontSize:13,bold:true,color:INK,margin:0});
  rows.forEach((r,i)=>{
    const y=2.6+i*0.72, me=i===0;
    if(me) s.addShape(p.ShapeType.roundRect,{x:9.25,y:y-0.08,w:3.2,h:0.62,
      fill:{color:"E4F7F0"},line:{color:"E4F7F0"},rectRadius:0.06});
    s.addText(r[0],{x:9.4,y:y,w:0.35,h:0.45,fontFace:"Arial",fontSize:13,bold:me,color:me?TEAL:GREY,margin:0});
    s.addText(r[1],{x:9.75,y:y,w:1.85,h:0.45,fontFace:KF,fontSize:13,bold:me,color:me?INK:GREY,margin:0});
    s.addText(r[2],{x:11.5,y:y,w:0.85,h:0.45,fontFace:"Arial",fontSize:13,bold:me,color:me?TEAL:GREY,align:"right",margin:0});
  });
  s.addNotes("Trial 번호는 대시보드 제출 순번. 8번의 유효 제출로 20.4점 상승.");
}

/* 3 ─ 핵심 결론 */
{ const s=dark();
  title(s,"하루 동안 배운 한 가지",null,true);
  s.addText("더한 것은 전부 점수를 깎았고,\n올린 것은 전부 호출 방식이었다",
    {x:0.9,y:2.0,w:W-1.8,h:1.8,fontFace:KF,fontSize:32,bold:true,color:WHITE,lineSpacing:46,margin:0});
  const items=[
    ["구조를 더함","RAG · 분류기 · 응급게이트 · 다중호출 · 외부화 CoT","전부 마이너스",ROSE],
    ["호출 방식을 바꿈","temperature · 지시 위치 · thinking 예산 · 출력 예산","+20.4점",MINT]];
  items.forEach((it,i)=>{
    const x=0.9+i*6.0;
    s.addShape(p.ShapeType.roundRect,{x,y:4.3,w:5.6,h:2.1,fill:{color:"14505F"},line:{color:"14505F"},rectRadius:0.1});
    s.addText(it[0],{x:x+0.35,y:4.55,w:5,h:0.4,fontFace:KF,fontSize:16,bold:true,color:WHITE,margin:0});
    s.addText(it[1],{x:x+0.35,y:5.0,w:5,h:0.8,fontFace:KF,fontSize:12,color:"9FC2CB",margin:0});
    s.addText(it[2],{x:x+0.35,y:5.85,w:5,h:0.4,fontFace:KF,fontSize:17,bold:true,color:it[3],margin:0});
  });
  s.addNotes("초기 풀 파이프라인(분류·검색·DUR·응급게이트)이 23.57점. 전부 걷어낸 raw 패스스루가 32.44점.");
}

/* 4 ─ 점수를 만든 네 가지 */
{ const s=light();
  title(s,"점수를 만든 네 가지","전부 코드 한 줄 수준의 변경");
  const it=[["+6.8","temperature: 0","서버 기본값(~1.0)으로 채점받고 있었다.\nrubric 438개 중 143개가 사실 정확성."],
            ["+3.8","지시를 user 턴에","system 메시지는 L2가 읽지 않는다.\n같은 문장을 위치만 옮김."],
            ["+6.7","thinking ON + fallback","추론은 끄는 게 아니라 지키는 것.\n손상된 것만 되살린다."],
            ["+1.5","max_tokens 6144","2048 상한은 우리가 건 족쇄였다.\n엔드포인트는 8192까지 받는다."]];
  it.forEach((d,i)=>{
    const x=0.7+(i%2)*6.2, y=1.95+Math.floor(i/2)*2.45;
    card(s,x,y,5.85,2.15);
    s.addText(d[0],{x:x+0.35,y:y+0.25,w:1.5,h:0.6,fontFace:"Arial",fontSize:28,bold:true,color:MINT,margin:0});
    s.addText(d[1],{x:x+1.9,y:y+0.32,w:3.75,h:0.5,fontFace:MF,fontSize:14,bold:true,color:INK,margin:0});
    s.addText(d[2],{x:x+0.35,y:y+1.0,w:5.2,h:0.95,fontFace:KF,fontSize:12,color:GREY,lineSpacing:17,margin:0});
  });
  s.addNotes("네 변경 모두 아키텍처가 아니라 API 호출 파라미터·프롬프트 위치.");
}

/* 5 ─ 발견 1: system vs user */
{ const s=light();
  title(s,"발견 ① L2는 system 메시지를 읽지 않는다","같은 지시를 위치만 바꿔 40문항씩 측정");
  const rows=[["지시 없음","10 / 40","",GREY],
              ["system 메시지","11 / 40","효과 없음",ROSE],
              ["user 턴 앞","2 / 40","",TEAL],
              ["user 턴 뒤","0 / 40","완전히 따름",MINT]];
  s.addText('지시: "Do not use emoji." · 측정: 답변에 이모지가 남은 비율',
    {x:0.7,y:1.85,w:7.5,h:0.35,fontFace:KF,fontSize:12,color:GREY,margin:0});
  rows.forEach((r,i)=>{
    const y=2.4+i*0.85;
    card(s,0.7,y,7.5,0.68, i===3?"E4F7F0":LIGHT);
    s.addText(r[0],{x:1.0,y:y+0.13,w:2.6,h:0.42,fontFace:KF,fontSize:14,bold:i===3,color:INK,margin:0});
    s.addText(r[1],{x:3.7,y:y+0.13,w:1.4,h:0.42,fontFace:"Arial",fontSize:15,bold:true,color:r[3],margin:0});
    s.addText(r[2],{x:5.3,y:y+0.15,w:2.6,h:0.4,fontFace:KF,fontSize:12,color:r[3],margin:0});
  });
  card(s,8.9,2.4,3.7,3.55,"0B3C49");
  s.addText("왜 중요했나",{x:9.2,y:2.65,w:3.2,h:0.35,fontFace:KF,fontSize:14,bold:true,color:MINT,margin:0});
  s.addText("초기 세 번의 시도가 프롬프트를 전부 system 에 넣었다.\n\n지시가 나빴던 게 아니라 아무 일도 일어나지 않고 있었다.\n\n같은 문장을 user 턴 끝으로 옮기자 +3.8점.",
    {x:9.2,y:3.15,w:3.2,h:2.5,fontFace:KF,fontSize:12,color:"C9DEE3",lineSpacing:18,margin:0});
  s.addNotes("Trial 7·8·13이 모두 system 프롬프트. Trial 22에서 user 턴으로 옮겨 43.07 달성.");
}

/* 6 ─ 발견 2: 출력 예산 */
{ const s=light();
  title(s,"발견 ② thinking 은 끄는 게 아니라 지키는 것","추론 토큰이 최종 답변과 같은 예산을 나눠 쓴다");
  const cols=[["thinking OFF","잘림 1/40","빈 응답 0/40","지연 6.4초","안전하지만 추론 없음",GREY],
              ["thinking ON (2048)","잘림 10/40","빈 응답 2/40","지연 19.6초","30% 손상",ROSE],
              ["ON + fallback","손상분만 복구","−","−","+6.7점",TEAL],
              ["ON + 6144 토큰","잘림 0/40","빈 응답 0/40","지연 동일","손상 0%",MINT]];
  cols.forEach((c,i)=>{
    const x=0.7+i*3.08;
    card(s,x,1.95,2.9,3.5, i===3?"E4F7F0":LIGHT);
    s.addText(c[0],{x:x+0.25,y:2.15,w:2.4,h:0.6,fontFace:MF,fontSize:12,bold:true,color:INK,margin:0});
    [1,2,3].forEach((k,j)=>s.addText(c[k],{x:x+0.25,y:2.85+j*0.45,w:2.4,h:0.4,fontFace:KF,fontSize:12,color:GREY,margin:0}));
    s.addText(c[4],{x:x+0.25,y:4.45,w:2.4,h:0.8,fontFace:KF,fontSize:14,bold:true,color:c[5],margin:0});
  });
  card(s,0.7,5.75,11.9,1.1,"0B3C49");
  s.addText("max_tokens 2048 은 팀이 스스로 건 제한이었다. 엔드포인트는 8192 까지 받는다 — 확인하지 않은 가정 하나가 22% 의 답변에서 추론을 버리게 하고 있었다.",
    {x:1.05,y:6.0,w:11.2,h:0.6,fontFace:KF,fontSize:13,color:"D6E8EC",margin:0});
  s.addNotes("CoEval 설정 파일도 max_tokens 6144를 기본으로 쓴다: thinking이 생성 문자의 34-59%.");
}

/* 7 ─ 기각 목록 */
{ const s=light();
  title(s,"데이터로 기각한 것들","그럴듯했고, 전부 측정에서 졌다");
  const rows=[["MCP / RAG 검색","인용 요구 기준 37개 중 0개 · 질문 96% 영어 · 도구는 한국 특화"],
              ["system 프롬프트","이모지 11/40 → 11/40. 행동 변화 없음"],
              ["외부화 2단 CoT","계획 호출 → 생성 호출. 73.3 → 65.4"],
              ["Best-of-N 선택","61.3 vs 62.9. 무효"],
              ["초안 3개 합성","6승 7패 27동률 · 재현 시 8승 9패"],
              ["thinking 재시도","회수율 45%, 그러나 0승 2패 18동률"],
              ["kNN few-shot","64.2 vs 65.4"],
              ["학회·지침 인용 금지","정확성 44.0 → 35.4. 모호해지며 가점까지 잃음"]];
  rows.forEach((r,i)=>{
    const y=1.95+i*0.62;
    s.addShape(p.ShapeType.roundRect,{x:0.7,y:y,w:0.28,h:0.28,fill:{color:ROSE},line:{color:ROSE},rectRadius:0.14});
    s.addText(r[0],{x:1.15,y:y-0.06,w:3.4,h:0.42,fontFace:KF,fontSize:13,bold:true,color:INK,margin:0});
    s.addText(r[1],{x:4.6,y:y-0.06,w:8.0,h:0.42,fontFace:KF,fontSize:12,color:GREY,margin:0});
  });
  s.addNotes("기각 근거를 문서로 남겨 팀원이 같은 길을 다시 파지 않게 했다.");
}

/* 8 ─ 측정 인프라 */
{ const s=light();
  title(s,"측정 인프라를 먼저 만들었다","대시보드 제출 1회 = 30분. 그 속도로는 실험이 불가능했다");
  const steps=[["01","리더보드 데이터셋 복원",
      "CoEval 공개 저장소의 val prompt-id 목록 + 공개 HealthBench Main\n→ 채점과 동일한 301문항 · 기준 3,410개 복원"],
    ["02","공식 채점식 구현",
      "Σ(met × points) / Σ(max(0, points)) · 문항 평균 후 [0,1] clip\n감점 기준 30.5% 포함"],
    ["03","축별 진단",
      "completeness · accuracy · context_awareness 별로 분해\n어디서 잃는지 문항 단위로 추적"]];
  steps.forEach((t,i)=>{
    const y=1.95+i*1.6;
    s.addShape(p.ShapeType.roundRect,{x:0.7,y:y,w:0.75,h:0.75,fill:{color:INK},line:{color:INK},rectRadius:0.1});
    s.addText(t[0],{x:0.7,y:y+0.17,w:0.75,h:0.42,fontFace:"Arial",fontSize:18,bold:true,color:MINT,align:"center",margin:0});
    s.addText(t[1],{x:1.7,y:y+0.02,w:4.2,h:0.45,fontFace:KF,fontSize:15,bold:true,color:INK,margin:0});
    s.addText(t[2],{x:1.7,y:y+0.5,w:10.6,h:0.8,fontFace:KF,fontSize:12,color:GREY,lineSpacing:17,margin:0});
  });
  card(s,0.7,6.55,11.9,0.62,"E4F7F0");
  s.addText("결과: 로컬 45.0 vs 리더보드 47.8 — 3점 이내로 캘리브레이션. 제출 없이 A/B 가능해졌다.",
    {x:1.05,y:6.68,w:11.2,h:0.4,fontFace:KF,fontSize:13,bold:true,color:INK,margin:0});
  s.addNotes("val은 test와 테마 구성·기준 구조가 맞춰져 있어 비편향 추정치. 특정 문항 최적화는 설계상 자기 처벌.");
}

/* 9 ─ 축별 진단 */
{ const s=light();
  title(s,"어디서 잃고 있었나","공식 루브릭 3,410개를 축별로 분해");
  const chart=[{name:"획득률",labels:["완전성","정확성","맥락인지","의사소통","지시순응"],values:[45.2,44.0,35.6,63.3,70.6]}];
  s.addChart(p.ChartType.bar,chart,{x:0.7,y:1.95,w:7.4,h:4.4,barDir:"col",
    showTitle:false,showLegend:false,chartColors:[TEAL,TEAL,ROSE,GREY,GREY],
    showValue:true,dataLabelPosition:"outEnd",dataLabelFontFace:"Arial",dataLabelFontSize:11,
    dataLabelColor:INK,dataLabelFormatCode:'0.0"%"',
    catAxisLabelColor:GREY,valAxisLabelColor:GREY,catAxisLabelFontSize:11,valAxisLabelFontSize:10,
    catAxisLabelFontFace:KF,valAxisLabelFontFace:"Arial",valAxisMaxVal:80,
    valGridLine:{color:"E3EAEC",size:1},catGridLine:{style:"none"}});
  card(s,8.6,1.95,4.0,4.4,"0B3C49");
  s.addText("배점 분포",{x:8.9,y:2.2,w:3.4,h:0.35,fontFace:KF,fontSize:14,bold:true,color:MINT,margin:0});
  const pts=[["완전성","1,450점"],["정확성","1,382점"],["맥락인지","711점"],["의사소통","245점"],["지시순응","209점"]];
  pts.forEach((r,i)=>{
    s.addText(r[0],{x:8.9,y:2.75+i*0.45,w:2.0,h:0.4,fontFace:KF,fontSize:12,color:"C9DEE3",margin:0});
    s.addText(r[1],{x:10.7,y:2.75+i*0.45,w:1.6,h:0.4,fontFace:"Arial",fontSize:12,color:WHITE,align:"right",margin:0});
  });
  s.addText("최대 배점 축(완전성)에서 45%,\n최저 획득 축(맥락인지)에 711점이 남아 있었다.\n\n감점 기준의 다수가 '누락'을 벌한다.",
    {x:8.9,y:5.15,w:3.4,h:1.0,fontFace:KF,fontSize:11,color:"9FC2CB",lineSpacing:16,margin:0});
  s.addNotes("감점 기준 1,039개 중 최대 배점들이 Fails to ... 형태. 짧게 쓰면 오히려 깎인다.");
}

/* 10 ─ 노이즈 */
{ const s=light();
  title(s,"가장 큰 적은 노이즈였다","같은 코드가 같은 점수를 내지 않는다");
  card(s,0.7,1.95,5.9,2.3);
  s.addText("동일 SHA, 두 번 제출",{x:1.05,y:2.2,w:5.2,h:0.4,fontFace:KF,fontSize:14,bold:true,color:INK,margin:0});
  s.addText("Trial 48",{x:1.05,y:2.75,w:2.0,h:0.4,fontFace:MF,fontSize:13,color:GREY,margin:0});
  s.addText("51.61",{x:3.1,y:2.72,w:1.3,h:0.45,fontFace:"Arial",fontSize:18,bold:true,color:GREY,margin:0});
  s.addText("Trial 49",{x:1.05,y:3.3,w:2.0,h:0.4,fontFace:MF,fontSize:13,color:GREY,margin:0});
  s.addText("52.88",{x:3.1,y:3.27,w:1.3,h:0.45,fontFace:"Arial",fontSize:18,bold:true,color:TEAL,margin:0});
  s.addText("차이 1.27점",{x:4.7,y:3.0,w:1.7,h:0.45,fontFace:KF,fontSize:14,bold:true,color:ROSE,margin:0});
  card(s,6.9,1.95,5.7,2.3);
  s.addText("그래서 바꾼 판정 방식",{x:7.25,y:2.2,w:5.0,h:0.4,fontFace:KF,fontSize:14,bold:true,color:INK,margin:0});
  s.addText("평균 대신 문항별 승·패·동률\n극단값 제거 후 재계산\n동일 설정 3회로 노이즈 바닥 측정\n3점 미만 차이는 채택하지 않음",
    {x:7.25,y:2.72,w:5.0,h:1.4,fontFace:KF,fontSize:12,color:GREY,lineSpacing:19,margin:0});
  card(s,0.7,4.55,11.9,2.1,"0B3C49");
  s.addText("실제로 걸러낸 사례",{x:1.05,y:4.8,w:5,h:0.4,fontFace:KF,fontSize:14,bold:true,color:MINT,margin:0});
  s.addText("한 프롬프트 변경이 평균 +2.5점으로 보였다. 문항별로 보니 한 문항이 만든 착시였고, 그 문항을 빼면 평균이 음수였다.\n원인은 문서 작성 과제였다 — 상담용 지시가 H&P 노트에 관리계획을 덧붙이게 만들어 감점 −8, −9를 동시에 밟았다. 그 문항 하나가 −242%.",
    {x:1.05,y:5.3,w:11.2,h:1.1,fontFace:KF,fontSize:12,color:"C9DEE3",lineSpacing:18,margin:0});
  s.addNotes("CoEval 문서도 n=500에서 sd 0.011, 약 3점 이내는 통계적 동률이라고 명시.");
}

/* 11 ─ 최종 구조 */
{ const s=light();
  title(s,"최종 제출 구조","남은 것은 이게 전부다");
  card(s,0.7,1.9,11.9,2.5,"0B3C49");
  const code=[
    "forwarded = messages[:]                              # 평가기 대화 그대로",
    'forwarded[-1]["content"] += f"\\n\\n[{ANSWER_INSTRUCTION}]"   # 지시는 user 턴에',
    "",
    "data = call_fm(forwarded, max_tokens=6144,           # 추론 예산 확보",
    "               temperature=0.0,                      # 사실 정확성",
    "               enable_thinking=True)                 # 추론 사용",
    "if damaged(data):                                    # 잘림·빈 응답이면",
    "    data = call_fm(forwarded, enable_thinking=False) # 추론 없이 재생성"];
  code.forEach((l,i)=>s.addText(l,{x:1.05,y:2.12+i*0.27,w:11.2,h:0.27,
    fontFace:MF,fontSize:11.5,color:i&&code[i]?"BFE3DC":"9FC2CB",margin:0}));
  const three=[["아키텍처 없음","분류기·검색·라우팅·다중호출 모두 제거"],
               ["실패해도 답한다","빈 응답은 0점. 어떤 경우에도 문장을 낸다"],
               ["예산을 지킨다","요청 단위 데드라인으로 타임아웃 0건"]];
  three.forEach((t,i)=>{
    const x=0.7+i*4.07;
    card(s,x,4.75,3.75,1.9);
    s.addText(t[0],{x:x+0.3,y:5.0,w:3.2,h:0.4,fontFace:KF,fontSize:14,bold:true,color:INK,margin:0});
    s.addText(t[1],{x:x+0.3,y:5.5,w:3.2,h:0.9,fontFace:KF,fontSize:12,color:GREY,lineSpacing:17,margin:0});
  });
  s.addNotes("ANSWER_INSTRUCTION: 진료 권유로 답을 대체하지 말고 아는 범위에서 답할 것.");
}

/* 12 ─ 방법론 */
{ const s=dark();
  title(s,"다음에도 쓸 방법","모델이 아니라 측정에 시간을 쓴다",true);
  const it=[["01","가정을 재고 시작한다","max_tokens 2048, MCP 40초, system 프롬프트 — 셋 다 사실이 아니었다"],
            ["02","노이즈 바닥을 먼저 잰다","동일 설정 반복 측정 없이는 어떤 개선도 판정할 수 없다"],
            ["03","평균이 아니라 승패를 본다","한 문항이 만든 착시를 두 번 걸러냈다"],
            ["04","기각을 기록한다","팀원이 같은 길을 다시 파지 않는다"],
            ["05","지는 것을 버린다","20.4점 중 대부분은 무언가를 뺐을 때 나왔다"]];
  it.forEach((t,i)=>{
    const y=1.95+i*0.98;
    s.addText(t[0],{x:0.75,y:y+0.05,w:0.7,h:0.45,fontFace:"Arial",fontSize:17,bold:true,color:MINT,margin:0});
    s.addText(t[1],{x:1.6,y:y,w:3.9,h:0.45,fontFace:KF,fontSize:15,bold:true,color:WHITE,margin:0});
    s.addText(t[2],{x:5.7,y:y+0.03,w:6.9,h:0.5,fontFace:KF,fontSize:12.5,color:"9FC2CB",margin:0});
  });
  s.addNotes("연구실 단위로 3개 팀을 낸 참가자를 4인 팀이 이겼다. 차이는 인원이 아니라 측정 공유였다.");
}

/* 13 ─ 마무리 */
{ const s=dark();
  s.addText("52.88",{x:0.9,y:1.9,w:6,h:1.5,fontFace:"Arial",fontSize:76,bold:true,color:MINT,margin:0});
  s.addText("HealthBench Consensus · 1위",{x:0.9,y:3.4,w:7,h:0.5,fontFace:KF,fontSize:20,color:WHITE,margin:0});
  s.addText("Lunit L2 (30B / 활성 5B MoE) 위에서\n파인튜닝 없이, 아키텍처 없이, 측정만으로.",
    {x:0.9,y:4.15,w:7.5,h:1.0,fontFace:KF,fontSize:15,color:"9FC2CB",lineSpacing:24,margin:0});
  const st=[["8","유효 제출"],["20.4","점 상승"],["301","문항 로컬 복원"],["3,410","기준 채점"]];
  st.forEach((d,i)=>{
    const x=0.9+i*3.0;
    s.addText(d[0],{x,y:5.6,w:2.6,h:0.6,fontFace:"Arial",fontSize:30,bold:true,color:WHITE,margin:0});
    s.addText(d[1],{x,y:6.2,w:2.6,h:0.35,fontFace:KF,fontSize:12,color:"7FA6B0",margin:0});
  });
  s.addNotes("감사합니다.");
}

p.writeFile({fileName:"AIM_ConquerHealth_2026.pptx"}).then(f=>console.log("생성:",f));
