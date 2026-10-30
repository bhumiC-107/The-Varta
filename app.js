const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const categories = ['All', 'World', 'India', 'Technology', 'Business', 'Science', 'Climate', 'Health', 'Society'];
let data = null, category = 'All', config = null, hiddenOpen = false, csrfToken = null, privateSession = false, sessionChecked = false, stateless = false;
const LOCAL_STATE_KEY='morning-signal-reader-v1';
const esc = value => String(value || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dateFormat = (value, options={}) => { if (!value) return 'Time unavailable'; const date = new Date(value); return Number.isNaN(+date) ? 'Time unavailable' : new Intl.DateTimeFormat(undefined, options).format(date); };
function ageLabel(value) {
  if(!value)return 'Date unverified';
  const age=Math.max(0,Date.now()-new Date(value).getTime()),hours=Math.floor(age/3600000);
  if(hours<1)return 'Published less than 1 hour ago';
  if(hours<24)return `Published ${hours} hour${hours===1?'':'s'} ago`;
  const days=Math.floor(hours/24);return `Published ${days} day${days===1?'':'s'} ago`;
}
function publicationMeta(s) {
  const published=s.published?`Published · ${esc(dateFormat(s.published,{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'}))}`:'<span class="date-unverified">Date unverified</span>';
  const fetched=s.fetched_at?`Fetched · ${esc(dateFormat(s.fetched_at,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'}))}`:'Fetch time unavailable';
  return `<span class="date-provenance">${published}<span>${fetched}</span></span>`;
}
function savedStoryFreshness(s) {
  if(!s.published)return 'Date unverified';
  const windowHours=Number(data?.settings?.freshness_hours)||48;
  return Date.now()-new Date(s.published).getTime()>windowHours*3600000?'Stale · outside briefing freshness window':ageLabel(s.published);
}

function browserState() {
  if(!stateless)return {stories:{},history:[],goal:1,difficulty:'Standard'};
  try { const value=JSON.parse(localStorage.getItem(LOCAL_STATE_KEY)||'{}');return {stories:value.stories||{},history:Array.isArray(value.history)?value.history:[],goal:Number(value.goal)||1,difficulty:['Quick scan','Standard','More context'].includes(value.difficulty)?value.difficulty:'Standard',morningBudget:[5,10,15].includes(Number(value.morningBudget))?Number(value.morningBudget):10}; }
  catch { return {stories:{},history:[],goal:1,difficulty:'Standard',morningBudget:10}; }
}
function writeBrowserState(state) {
  try { localStorage.setItem(LOCAL_STATE_KEY,JSON.stringify(state));return true; }
  catch { $('#status').textContent='Browser storage is unavailable. Your changes may not survive a reload.';return false; }
}
function saveBrowserStory(story, change) {
  const state=browserState(),old=state.stories[story.id]||{};
  if(old.hidden&&change.hidden===false&&data&&!data.stories.some(item=>item.id===story.id)&&story.date_status==='recent'&&story.qualifies)data.stories.push(story);
  const copy={...story,...old,...change};
  copy.snapshot={...story,...(old.snapshot||{}),...change};
  state.stories[story.id]=copy;
  if(!copy.read&&!copy.saved&&!copy.hidden&&!copy.note&&!copy.feedback)delete state.stories[story.id];
  writeBrowserState(state);
  applyBrowserState(data);
  render();
}
function localDateKey(date=new Date()) { return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`; }
function safeArticleUrl(value) { try { const url=new URL(value);return url.protocol==='https:'&&!url.username&&!url.password; } catch { return false; } }
function storyTimeAllowance(story) {
  const sourceMinutes=Number(story.reading_minutes);
  return Number.isInteger(sourceMinutes)&&sourceMinutes>=1&&sourceMinutes<=120
    ? {minutes:sourceMinutes,label:'Publisher estimate'}
    : {minutes:2,label:'2-minute planning allowance'};
}
function applyReaderMode(story, mode) {
  const c=story?.context;if(!c)return story;
  const original=c.original_publisher&&safeArticleUrl(c.original_publisher.url)?c.original_publisher:{name:(story.sources||[])[0]?.name||'Original publisher',url:story.url};
  const others=(c.other_publishers||[]).filter(source=>safeArticleUrl(source.url));
  story.display_context=mode==='Quick scan'?{mode,summary:c.quick_summary||c.what_happened,summary_label:c.quick_summary_label||c.what_happened_label,original_publisher:original,other_publishers:[],not_clear:[],publisher_excerpt:''}:
    mode==='More context'?{mode,summary:c.what_happened,summary_label:c.what_happened_label,why_matter:c.why_matter,not_clear:c.not_clear||[],publisher_excerpt:c.publisher_excerpt,publisher_excerpt_attribution:c.publisher_excerpt_attribution,original_publisher:original,other_publishers:others}:
    {mode:'Standard',summary:c.what_happened,summary_label:c.what_happened_label,why_matter:c.why_matter,not_clear:[],publisher_excerpt:'',original_publisher:original,other_publishers:[]};
  return story;
}
function applyBrowserState(payload) {
  if(!stateless||!payload)return payload;
  const state=browserState(),records=state.stories;
  const decorate=story=>{const saved=records[story.id];return saved?{...story,read:!!saved.read,saved:!!saved.saved,hidden:!!saved.hidden,feedback:saved.feedback||null,note:saved.note||null,read_date:saved.read_date||null}:story;};
  const candidates=(payload.stories||[]).map(decorate).filter(story=>!story.hidden),budgeted=[];let budgetUsed=0;
  for(const story of candidates){const allowance=storyTimeAllowance(story);if(budgetUsed+allowance.minutes>state.morningBudget)continue;budgetUsed+=allowance.minutes;budgeted.push({...story,budget_minutes:allowance.minutes,budget_time_label:allowance.label});}
  payload.stories=budgeted.map(story=>applyReaderMode(story,state.difficulty));
  payload.unverifiedStories=(payload.unverifiedStories||[]).map(decorate).map(story=>applyReaderMode(story,state.difficulty)).filter(story=>!story.hidden);
  payload.hiddenStories=Object.values(records).filter(story=>story.hidden).map(story=>story.snapshot||story).filter(story=>safeArticleUrl(story.url));
  const saved=Object.values(records).filter(story=>story.saved||story.note).map(story=>story.snapshot||story).filter(story=>safeArticleUrl(story.url)).map(decorate).map(story=>applyReaderMode(story,state.difficulty)).sort((a,b)=>String(b.published||'').localeCompare(String(a.published||'')));
  const today=localDateKey(),readToday=Object.values(records).filter(story=>story.read&&story.read_date===today).length;
  payload.reading={...(payload.reading||{}),goal:state.goal,read_today:readToday,
    deep_read_completed:state.history.some(item=>item.date===today&&item.deep),saved,
    history:state.history.filter(item=>item.date>=localDateKey(new Date(Date.now()-6*86400000))&&safeArticleUrl(item.url)).sort((a,b)=>b.date.localeCompare(a.date))};
  payload.settings={...(payload.settings||{}),difficulty:state.difficulty,daily_goal:state.goal,morning_budget:state.morningBudget};
  payload.selection={...(payload.selection||{}),morning_budget:state.morningBudget,budget_used_minutes:budgetUsed,budget_limited:budgeted.length<candidates.length};
  if(!budgeted.length&&candidates.length){payload.selection.empty_reason=`No selected stories fit your ${state.morningBudget}-minute scan budget.`;payload.selection.empty_next_steps=['Increase the morning budget in preferences','Refresh feeds for shorter or better-timed reports'];}
  if(payload.deepRead){payload.deepRead=applyReaderMode(decorate(payload.deepRead),state.difficulty);payload.deepReadCompleted=payload.reading.deep_read_completed;}
  payload.hiddenStories=payload.hiddenStories.map(story=>applyReaderMode(story,state.difficulty));
  return payload;
}

async function ensureSession() {
  if(sessionChecked)return;
  const res=await fetch('/api/session',{credentials:'same-origin',headers:{'Accept':'application/json'}});
  if(res.status===404){sessionChecked=true;return;}
  if(res.status===401){window.location.replace('/');throw new Error('Sign in to continue.');}
  if(!res.ok)throw new Error(`Session check failed (${res.status})`);
  const session=await res.json();csrfToken=session.csrf||null;privateSession=session.private===true;stateless=session.stateless===true;sessionChecked=true;
  $('#sign-out').hidden=!privateSession;
  if(stateless){$('#sign-out').hidden=true;$('#stateless-note').hidden=false;$('#stateless-note').textContent='Your reading list, notes, and progress save only in this browser. Feed refresh sends no reading activity or preferences.';$$('#settings-form fieldset').forEach(el=>el.hidden=true);$('#length').closest('label').hidden=true;$('#freshness-hours').closest('label').hidden=true;$('#settings-description').textContent='Your reading goal and display mode are stored only in this browser. No account or cloud database is used.';}
}
async function request(path, body) {
  if(body)await ensureSession();
  const headers={};
  if(body){headers['Content-Type']='application/json';if(csrfToken)headers['X-CSRF-Token']=csrfToken;}
  const res = await fetch(path, body ? {method:'POST', headers, body:JSON.stringify(body),credentials:'same-origin'} : {credentials:'same-origin'});
  if (!res.ok) throw new Error(`Request failed (${res.status})`);
  if(res.status===204)return {};
  return res.json();
}
function drawFilters() {
  $('#filters').innerHTML = categories.map(c => `<button class="filter ${c===category?'active':''}" data-category="${esc(c)}" aria-pressed="${c===category}">${esc(c)}</button>`).join('');
}
function storyContextMarkup(story) {
  const context=story.display_context||story.context||{}, mode=context.mode||'Standard';
  const other=(context.other_publishers||[]).map(source=>source.url?`<li><a href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.name)} — independent coverage ↗</a></li>`:'').join('');
  const original=context.original_publisher||{name:(story.sources||[])[0]?.name||'Original publisher',url:story.url};
  const uncertainty=(context.not_clear||[]).length?`<section class="context-field"><h4>What’s not clear yet</h4><ul>${context.not_clear.map(item=>`<li>${esc(item)}</li>`).join('')}</ul></section>`:'';
  const why=context.why_matter?`<section class="context-field"><h4>Why it may matter</h4><p>${esc(context.why_matter)}</p></section>`:'';
  const excerpt=context.publisher_excerpt?`<section class="context-field publisher-excerpt"><h4>Available feed excerpt</h4><blockquote>${esc(context.publisher_excerpt)}</blockquote><small>${esc(context.publisher_excerpt_attribution)}</small></section>`:'';
  const indiaLens=mode!=='Quick scan'&&story.india_lens?`<details class="india-lens"><summary>India lens <span>context, not a prediction</span></summary><p>${esc(story.india_lens)}</p></details>`:'';
  return `<div class="story-context mode-${mode==='Quick scan'?'quick':mode==='More context'?'more':'standard'}"><section class="context-field"><h4>${mode==='Quick scan'?'Very short feed summary':'What happened'}</h4><p>${esc(context.summary||'The feed does not provide enough information for a reliable summary. Open the publisher report for context.')}</p><small>${esc(context.summary_label||'Feed-based summary. Open the publisher report for more context.')}</small></section>${why}${uncertainty}${excerpt}${indiaLens}<section class="context-field sources-field"><h4>Sources</h4><ul><li><a class="original-source-link" href="${esc(original.url||story.url)}" target="_blank" rel="noopener noreferrer">Original report — ${esc(original.name)} ↗</a></li>${other||mode==='More context'?other||'<li>No other independent publisher is attached to this story.</li>':''}</ul></section></div>`;
}
function termMarkup(story) {
  const terms=(story.related_terms||[]).slice(0,3);
  if(!terms.length)return '';
  return `<div class="term-links"><span>Quick explanations</span>${terms.map(term=>`<button type="button" class="term-button" aria-haspopup="dialog" aria-label="Explain ${esc(term.term)} with a cited reference" data-term-story="${esc(story.id)}" data-term-id="${esc(term.id)}">${esc(term.term)} <span aria-hidden="true">ⓘ</span></button>`).join('')}</div>`;
}
function storyTimeMarkup(story) {
  if(!story.budget_minutes)return '';
  return `<span class="budget-time" title="${esc(story.budget_time_label)}">${esc(story.budget_minutes)} min · ${esc(story.budget_time_label)}</span>`;
}
function articleCard(s, unverified=false) {
  const context=storyContextMarkup(s);
  const mode=data?.settings?.difficulty||'Standard';
  const note=s.read?`<div class="note-editor"><label for="note-${esc(s.id)}">Personal note</label><textarea id="note-${esc(s.id)}" maxlength="280" placeholder="A thought to remember…">${esc(s.note||'')}</textarea><button class="small-action" data-save-note="${esc(s.id)}" aria-label="Save personal note for ${esc(s.title)}">Save note</button></div>`:'';
  const status=s.date_status==='stale'?'Stale':unverified||s.date_status==='unverified'||!s.published?'Date unverified':'';
  const signals=s.rank_signals||[];
  const explanations=(s.why_this_is_here||[]).length?s.why_this_is_here:['No individual ranking signal stood out.'];
  const breakdown=signals.length?signals.map(x=>`<li><span>${esc(x.label)}</span><b>${x.score>0?'+':''}${Number(x.score).toFixed(2)}</b></li>`).join(''):'<li>No non-zero ranking signals applied.</li>';
  const reason=`<div class="why-here"><b>Why this is here</b><span>${explanations.map(esc).join(' · ')}</span></div><details class="score-details"><summary>Score breakdown</summary><p>These are rule-based score contributions, not a measure of truth or certainty.</p><ul>${breakdown}</ul></details>`;
  const feedback=stateless?'':`<div class="feedback-actions" role="group" aria-label="Local relevance feedback for ${esc(s.title)}"><button class="small-action" data-action="feedback-more" aria-pressed="${s.feedback==='more'}" aria-label="${s.feedback==='more'?'Remove More like this preference':'More like this'} for ${esc(s.title)}">${s.feedback==='more'?'✓ More like this':'More like this'}</button><button class="small-action" data-action="feedback-less" aria-pressed="${s.feedback==='less'}" aria-label="${s.feedback==='less'?'Remove Less like this preference':'Less like this'} for ${esc(s.title)}">${s.feedback==='less'?'✓ Less like this':'Less like this'}</button>${s.feedback?'<button class="small-action" data-action="feedback-clear">Undo preference</button>':''}</div><p class="feedback-help">These choices stay on this device. They adjust this story and give a small boost or reduction to matching topics and regions. Undo clears this story’s feedback.</p>`;
  const actions=`<div class="actions" role="group" aria-label="Story actions for ${esc(s.title)}"><button class="small-action ${s.saved?'on':''}" data-action="saved" aria-label="${s.saved?'Remove saved status from':'Save'} ${esc(s.title)}">${s.saved?'★ Saved':'☆ Save'}</button><button class="small-action" data-action="read" aria-label="Mark ${esc(s.title)} ${s.read?'unread':'read'}">${s.read?'✓ Read':'○ Read'}</button><button class="small-action" data-action="hidden" aria-label="Hide ${esc(s.title)}">× Hide</button></div>`;
  return `<article class="story" data-story="${esc(s.id)}"><div class="story-top"><span class="category">${esc(s.category)} · ${esc(s.region)}${status?` · <span class="date-status">${esc(status)}</span>`:''}</span>${actions}</div><h3>${esc(s.title)}</h3>${storyTimeMarkup(s)}${mode==='Quick scan'?'':reason}${termMarkup(s)}${feedback}${context}${note}<div class="story-foot">${publicationMeta(s)}</div></article>`;
}

function readingListCard(s) {
  const note=s.read?`<div class="note-editor"><label for="list-note-${esc(s.id)}">Personal note</label><textarea id="list-note-${esc(s.id)}" maxlength="280" placeholder="A thought to remember…">${esc(s.note||'')}</textarea><button class="small-action" data-save-note="${esc(s.id)}" aria-label="Save personal note for ${esc(s.title)}">Save note</button></div>`:'';
  const publisher=(s.sources||[]).map(x=>x.name).join(' · ')||'Publisher link';
  return `<article class="reading-list-item" data-story="${esc(s.id)}"><div><span class="category">${esc(s.category)} · ${esc(s.region)}</span><h3><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a></h3><p>${esc(publisher)} · ${esc(savedStoryFreshness(s))} · Fetched ${esc(dateFormat(s.fetched_at,{month:'short',day:'numeric'}))}</p>${termMarkup(s)}</div><div class="reading-list-actions" role="group" aria-label="Reading list actions for ${esc(s.title)}"><button class="small-action" data-reading-action="read" data-id="${esc(s.id)}" aria-label="Mark ${esc(s.title)} ${s.read?'unread':'read'}">${s.read?'✓ Read':'○ Mark read'}</button><button class="small-action" data-reading-action="saved" data-id="${esc(s.id)}" aria-label="${s.saved?'Remove':'Save'} ${esc(s.title)} ${s.saved?'from':'to'} your reading list">${s.saved?'Remove':'Save for later'}</button></div>${note}</article>`;
}

function hiddenStoryCard(s) {
  const original=(s.sources||[]).find(source=>source.url===s.url)||(s.sources||[])[0]||{};
  const publisher=original.name||'Publisher unavailable';
  const date=s.published?dateFormat(s.published,{weekday:'short',month:'short',day:'numeric',year:'numeric'}):'Date unverified';
  const readStatus=s.read?'Read':'Unread';
  return `<article class="hidden-story"><div class="hidden-story-head"><span class="hidden-badge">Hidden</span><span class="read-badge">${readStatus}</span></div><h3><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a></h3><p>${esc(publisher)} · ${esc(date)}</p><div class="hidden-story-actions"><a class="original-source-link" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">Open original article ↗</a><button class="small-action" data-hidden-restore="${esc(s.id)}" aria-label="Restore ${esc(s.title)} to the briefing">Restore to briefing</button></div></article>`;
}

function renderHiddenStories() {
  const hidden=data.hiddenStories||[];
  $('#hidden-toggle').textContent=`Hidden stories (${hidden.length})`;
  $('#hidden-section').classList.toggle('hidden',!hiddenOpen);
  $('#hidden-toggle').setAttribute('aria-expanded',String(hiddenOpen));
  $('#hidden-list').innerHTML=hidden.length?hidden.map(hiddenStoryCard).join(''):'<div class="empty compact-empty" style="grid-column:1/-1"><strong>No hidden stories</strong><p>Stories you hide are kept here, separate from stories marked read. You can restore one to the briefing whenever you like.</p></div>';
}

function renderReading() {
  const reading=data.reading||{goal:1,read_today:0,deep_read_completed:false,history:[],saved:[]};
  const goal=Number(reading.goal)||1, completed=Number(reading.read_today)||0, pct=Math.min(100,Math.round(completed/goal*100));
  $('#reading-progress-label').textContent=`${completed} of ${goal} ${goal===1?'article':'articles'}`;
  $('#reading-progress-bar').style.width=`${pct}%`;
  const progress=$('.progress-track');progress.setAttribute('aria-valuemax',String(goal));progress.setAttribute('aria-valuenow',String(Math.min(completed,goal)));
  const deep=data.deepRead;
  $('#deep-wrap').innerHTML=deep?`<article class="today-card" data-story="${esc(deep.id)}"><div class="deep-label">✳ &nbsp;${esc(data.focus.toUpperCase())} · ${esc(ageLabel(deep.published))}</div><h3>${esc(deep.title)}</h3>${termMarkup(deep)}${storyContextMarkup(deep)}<div class="today-meta">${publicationMeta(deep)}${deep.reading_minutes?`<span>${esc(deep.reading_minutes)} min read</span>`:''}</div><div class="today-actions"><a class="button-primary" href="${esc(deep.url)}" target="_blank" rel="noopener noreferrer">Read at publisher ↗</a>${reading.deep_read_completed?'<span class="completed-label">✓ Deep read completed today</span>':'<button class="text-button" id="complete-deep">Mark deep read complete</button>'}</div></article>`:'<div class="empty reading-empty"><strong>No suitable recent deep read</strong><p>No dated, recent article matched today’s “'+esc(data.focus)+'” theme. Refresh feeds or check back later; the app will not substitute an unrelated or stale story.</p></div>';
  const saved=reading.saved||[],savedCount=saved.filter(item=>item.saved).length;$('#saved-count').textContent=savedCount?`${savedCount} saved`:'';
  $('#reading-list').innerHTML=saved.length?saved.map(readingListCard).join(''):'<div class="empty compact-empty"><strong>Your list is clear</strong><p>Save a story from the briefing and it will be here for later.</p></div>';
  const history=reading.history||[];
  $('#reading-history').innerHTML=history.length?`<ol class="history-list">${history.map(item=>`<li><time datetime="${esc(item.date)}">${esc(dateFormat(`${item.date}T12:00:00`,{weekday:'short',month:'short',day:'numeric'}))}</time><span>${esc(item.theme)}</span><a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a></li>`).join('')}</ol>`:'<div class="empty compact-empty"><strong>No completed deep reads this week</strong><p>Finish a featured article to start your weekly history.</p></div>';
  const readDates=new Set([...history.map(item=>item.date),...Object.values(browserState().stories).filter(item=>item.read&&item.read_date).map(item=>item.read_date)]);
  let streak=0, cursor=new Date();if(!readDates.has(localDateKey(cursor))){cursor.setDate(cursor.getDate()-1);if(!readDates.has(localDateKey(cursor)))cursor=null;}
  while(cursor&&readDates.has(localDateKey(cursor))){streak++;cursor.setDate(cursor.getDate()-1);}
  $('#reading-streak').textContent=`${streak} day${streak===1?'':'s'} in a row`;
}
function render() {
  if (!data) return;
  const s = data.stories;
  $('#date-label').textContent = dateFormat(new Date(), {weekday:'long',month:'long',day:'numeric'});
  $('#today').textContent = dateFormat(new Date(), {weekday:'long'}).toUpperCase() + ' · YOUR MORNING BRIEFING';
  $('#focus-title').textContent = data.focus;
  $('#focus-day').textContent = dateFormat(new Date(),{weekday:'long'}).toUpperCase();
  $('#story-count').textContent = `${s.length} ${s.length===1?'story':'stories'} in your briefing`;
  drawFilters();
  renderHiddenStories();
  renderReading();
  drawCoverage();
  const filtered = category==='All' ? s : s.filter(x=>x.category===category);
  $('#story-list').innerHTML = filtered.map(articleCard).join('');
  if (s.length && !filtered.length) $('#story-list').innerHTML = `<div class="empty" style="grid-column:1/-1"><strong>No ${esc(category)} stories in this briefing.</strong><p>Try another category or refresh the enabled feeds for newer coverage.</p></div>`;
  const q=data.selection||{freshness_hours:48,fresh_count:s.length,stale_count:0,undated_count:0,requested:10};
  const budget=Number(q.morning_budget||data.settings?.morning_budget)||10,used=Number(q.budget_used_minutes)||0;
  $('#budget-summary').textContent=`${budget}-minute scan budget · ${used} min allocated${q.budget_limited?' · shorter selections fit the budget':''}. Publisher read times are used when available; otherwise each item gets a 2-minute planning allowance.`;
  $('#status').textContent=s.length<q.requested?`Showing ${s.length} stories from ${q.fresh_count} dated, recent relevant candidates (requested ${q.requested}); ${q.budget_limited?'the morning time budget also limits the selection.':'remaining items fell outside the top ranking band or freshness rules.'} ${q.stale_count} stale stories excluded. ${q.undated_count} date-unverified stories are separate.`:`Ranked by publication recency, your selected topics and regions, India coverage, and distinct publisher reporting. ${q.stale_count} stale stories excluded; ${q.undated_count} undated stories are separate.`;
  $('#story-count').textContent = s.length ? `${s.length} ${s.length===1?'story':'stories'} in your briefing` : 'No stories yet';
  if (!s.length) {
    const next=(q.empty_next_steps||[]).map(x=>`<li>${esc(x)}</li>`).join('');
    $('#story-list').innerHTML = `<div class="empty" style="grid-column:1/-1"><strong>${esc(q.empty_reason||'No stories currently qualify for the briefing.')}</strong><p>${q.stale_count||0} stale stories were excluded; ${q.undated_count||0} date-unverified items are kept separate. The current freshness window is ${q.freshness_hours} hours. The briefing stays short when no dated, relevant stories qualify.</p>${next?`<ul class="empty-next-steps">${next}</ul>`:''}<button id="empty-refresh">Refresh feeds</button></div>`;
  }
  const unverified=data.unverifiedStories||[];
  const shownUnverified=unverified.filter(story=>category==='All'||story.category===category);
  $('#undated-wrap').classList.toggle('hidden',!shownUnverified.length);
  $('#undated-list').innerHTML=shownUnverified.map(story=>articleCard(story,true)).join('');
  drawSources();
}
function drawSources() {
  const panel = $('#source-panel');
  panel.innerHTML = `<strong style="font-size:12px">Configured sources</strong>` + data.sources.map(s => {
    const st=s.status, name=esc(s.name), status=st?.error?`Failed · ${esc(st.error)} · checked ${esc(dateFormat(st.checked,{hour:'numeric',minute:'2-digit'}))}`:st?.checked?`Updated ${esc(dateFormat(st.checked,{hour:'numeric',minute:'2-digit'}))}`:s.verification==='verified'?'Not fetched yet':'Unverified';
    return `<div class="source-row"><div><b>${name}</b><br><small>${esc(s.publisher||s.name)} · ${esc(s.topic)} · ${esc(s.region)}${s.region_fallback?' (section fallback)':''} · ${esc(s.verification)} · ${esc(s.terms)}</small></div><span class="source-state ${st?.error?'failed':''}">${status}</span></div>`;
  }).join('');
}
function drawCoverage() {
  const coverage=data.coverage||{checked_today:0,failed_today:0,story_count:0,regions:[],publishers:[]};
  const panel=$('#coverage-indicator');
  if(!coverage.checked_today){
    panel.innerHTML='<p>No source checks are recorded for today. Refresh feeds to calculate which publishers and regions contribute to this briefing.</p><p class="coverage-note">This is a record of selected-story sources, not a claim of global balance.</p>';
    return;
  }
  const publishers=(coverage.publishers||[]).map(p=>`<span class="coverage-chip">${esc(p.name)}</span>`).join('');
  const regions=(coverage.regions||[]).map(region=>`<span class="coverage-chip region-chip">${esc(region)}</span>`).join('');
  const limited=(coverage.publishers||[]).length<3||(coverage.regions||[]).length<3;
  const status=coverage.story_count?`${coverage.story_count} selected ${coverage.story_count===1?'story':'stories'} came from ${coverage.publishers.length} independent ${coverage.publishers.length===1?'publisher':'publishers'} across ${coverage.regions.length} classified ${coverage.regions.length===1?'region':'regions'}.`:'No selected story has a source that both succeeded and supplied it today.';
  panel.innerHTML=`<p>${esc(status)}${coverage.failed_today?` ${esc(coverage.failed_today)} feed ${coverage.failed_today===1?'failed':'feeds failed'} today and ${coverage.failed_today===1?'is':'are'} not counted.`:''}</p><div class="coverage-groups"><div><b>Publishers</b><div class="coverage-chips">${publishers||'<span class="coverage-muted">None yet</span>'}</div></div><div><b>Regions</b><div class="coverage-chips">${regions||'<span class="coverage-muted">None yet</span>'}</div></div></div><p class="coverage-note">${limited?'Coverage is limited today. ':' '}These regions describe selected stories from successful feeds; they do not guarantee a globally balanced briefing.</p>`;
}
async function load() {
  if (window.location.protocol === 'file:') {
    $('#story-count').textContent = 'Local server required';
    $('#status').textContent = '';
    $('#file-open-notice').hidden = false;
    $('#refresh').disabled = true;
    $('#settings-open').disabled = true;
    return;
  }
  $('#status').textContent='Loading your briefing…';
  try { await ensureSession();[data,config] = await Promise.all([request('/api/briefing'),request('/api/config')]);if(stateless){$('#status').textContent='Checking your sources and collecting fresh headlines…';await refreshFeeds(true);}else render(); }
  catch(e) { $('#status').textContent=`Could not load your briefing: ${e.message}`; }
}
function options(container, values, selected, name, labels={}) {
  $(container).innerHTML = values.map(v=>`<label class="check"><input type="checkbox" name="${name}" value="${esc(v)}" ${selected.includes(v)?'checked':''}>${esc(labels[v]||v)}</label>`).join('');
}
function openSettings() {
  const p=data.settings, sourceIds=Array.isArray(p.sources)?p.sources:config.sources.filter(s=>s.enabled&&s.verification==='verified').map(s=>s.id);
  options('#region-options',['India','Asia','Europe','Africa','Americas','Middle East','Global'],p.regions,'regions');
  options('#topic-options',['World','India','Technology','Business','Science','Climate','Health','Society'],p.topics,'topics');
  const eligibleSources=config.sources.filter(s=>s.enabled&&s.verification==='verified');
  options('#source-options',eligibleSources.map(s=>s.id),sourceIds,'sources',Object.fromEntries(eligibleSources.map(s=>[s.id,s.name])));
  options('#excluded-options',p.excluded||[],p.excluded||[],'excluded');
$('#length').value=String(p.length||10); $('#freshness-hours').value=String(p.freshness_hours||48); $('#difficulty').value=p.difficulty||'Standard'; $('#daily-goal').value=String(p.daily_goal||1); $('#morning-budget').value=String(p.morning_budget||10); $('#save-note').textContent='';
  $('#settings-dialog').showModal();
}
function openTermExplanation(storyId, termId) {
  const stories=[...(data?.stories||[]),...(data?.unverifiedStories||[]),...(data?.reading?.saved||[]),...(data?.hiddenStories||[]),...(data?.deepRead?[data.deepRead]:[])];
  const definition=stories.find(story=>story.id===storyId)?.related_terms?.find(term=>term.id===termId);
  if(!definition||!safeArticleUrl(definition.source_url))return;
  $('#term-title').textContent=definition.term;
  $('#term-description').textContent=definition.explanation;
  $('#term-attribution').textContent=`Reference: ${definition.source_name}`;
  const link=$('#term-source-link');link.href=definition.source_url;link.textContent=`Open ${definition.source_name} ↗`;
  $('#term-dialog').showModal();
}
function handleTermClick(event) {
  const button=event.target.closest('[data-term-story][data-term-id]');
  if(button)openTermExplanation(button.dataset.termStory,button.dataset.termId);
}
$('#settings-open').addEventListener('click',()=>{if(data&&config)openSettings();});
$('#sign-out').addEventListener('click',async()=>{try{await request('/api/logout',{});window.location.assign('/');}catch(e){$('#status').textContent=`Could not sign out: ${e.message}`;}});
$('#save-settings').addEventListener('click',async()=>{
  if(stateless){const state=browserState();state.goal=Number($('#daily-goal').value)||1;state.difficulty=$('#difficulty').value;state.morningBudget=Number($('#morning-budget').value)||10;writeBrowserState(state);applyBrowserState(data);render();$('#save-note').textContent='Saved in this browser';$('#settings-dialog').close();return;}
  const val=name=>$$(`input[name="${name}"]:checked`).map(el=>el.value);
  try { await request('/api/settings',{regions:val('regions'),topics:val('topics'),sources:val('sources'),excluded:val('excluded'),length:Number($('#length').value),freshness_hours:Number($('#freshness-hours').value),difficulty:$('#difficulty').value,daily_goal:Number($('#daily-goal').value),morning_budget:Number($('#morning-budget').value)}); $('#save-note').textContent='Saved'; $('#settings-dialog').close(); await load(); }
  catch(e){$('#save-note').textContent=`Could not save: ${e.message}`;}
});
$('#filters').addEventListener('click',e=>{const b=e.target.closest('[data-category]');if(b){category=b.dataset.category;drawFilters();render();}});
async function handleStoryAction(e) {
  const noteButton=e.target.closest('[data-save-note]');
  if(noteButton){await saveNote(noteButton.dataset.saveNote,noteButton.closest('.note-editor').querySelector('textarea').value);return;}
  const btn=e.target.closest('[data-action]'); if(!btn)return;
  const card=btn.closest('[data-story]'), story=[...data.stories,...(data.unverifiedStories||[])].find(s=>s.id===card.dataset.story); if(!story)return;
  const field=btn.dataset.action;
  let change;
  if(field==='feedback-more'||field==='feedback-less') { const next=field==='feedback-more'?'more':'less'; change={feedback:story.feedback===next?'clear':next}; }
  else if(field==='feedback-clear') { change={feedback:'clear'}; }
  else change={[field]:field==='read'?!story.read:field==='saved'?!story.saved:true};
  if(field==='hidden'&&change.hidden)hiddenOpen=true;
  if(stateless){if(field==='read')change.read_date=change.read?localDateKey():null;saveBrowserStory(story,change);$('#status').textContent=field==='hidden'?'Story hidden. Review it in Hidden stories.':field==='saved'?(change.saved?'Saved to your reading list on this browser.':'Removed from your reading list.'):field==='read'?(change.read?'Marked read.':'Marked unread.'):'';return;}
  try{
    await request(`/api/story/${story.id}`,change);await load();
    const message=field==='hidden'?'Story hidden. Review it in Hidden stories.':field==='saved'?(change.saved?'Story saved to your reading list.':'Story removed from your reading list.'):field==='read'?(change.read?'Story marked read.':'Story marked unread.'):field==='feedback-clear'?'Local relevance preference cleared.':field.startsWith('feedback-')?(change.feedback==='clear'?'Local relevance preference cleared.':'Local relevance preference updated for this story and similar topics and regions.'):'';
    if(message)$('#status').textContent=message;
  }catch(err){$('#status').textContent=`Could not update story: ${err.message}`;}
}
$('#story-list').addEventListener('click',handleStoryAction);
$('#undated-list').addEventListener('click',handleStoryAction);
$('#story-list').addEventListener('click',handleTermClick);
$('#reading-list').addEventListener('click',handleTermClick);
$('#deep-wrap').addEventListener('click',handleTermClick);
$('#term-close').addEventListener('click',()=>$('#term-dialog').close());
async function saveNote(id,note){
  if(stateless){const story=[...(data.stories||[]),...(data.unverifiedStories||[]),...(data.reading?.saved||[]),...(data.hiddenStories||[])].find(item=>item.id===id);if(!story)return;if(!story.read){$('#status').textContent='Mark this story read before adding a note.';return;}saveBrowserStory(story,{note:String(note).slice(0,280)});$('#status').textContent='Personal note saved in this browser.';return;}
  try{await request(`/api/story/${id}`,{note});await load();$('#status').textContent='Personal note saved on this device.';}
  catch(err){$('#status').textContent=`Could not save note: ${err.message}`;}
}
$('#reading-list').addEventListener('click',async e=>{
  const noteButton=e.target.closest('[data-save-note]');
  if(noteButton){await saveNote(noteButton.dataset.saveNote,noteButton.closest('.note-editor').querySelector('textarea').value);return;}
  const button=e.target.closest('[data-reading-action]');if(!button)return;
  const id=button.dataset.id,field=button.dataset.readingAction;
  const story=(data.reading.saved||[]).find(item=>item.id===id);if(!story)return;
  if(stateless){const change=field==='read'?{read:!story.read,read_date:!story.read?localDateKey():null}:{saved:!story.saved};saveBrowserStory(story,change);$('#status').textContent=field==='read'?(change.read?'Marked read.':'Marked unread.'):(change.saved?'Saved for later.':'Removed from your reading list.');return;}
  try{await request(`/api/story/${id}`,{[field]:field==='read'?!story.read:!story.saved});await load();$('#status').textContent=field==='read'?(story.read?'Story marked unread.':'Story marked read.'):(story.saved?'Story removed from your reading list.':'Story saved to your reading list.');}
  catch(err){$('#status').textContent=`Could not update reading list: ${err.message}`;}
});
$('#hidden-toggle').addEventListener('click',()=>{hiddenOpen=!hiddenOpen;renderHiddenStories();});
$('#hidden-list').addEventListener('click',async e=>{
  const button=e.target.closest('[data-hidden-restore]');if(!button)return;
  const storyId=button.dataset.hiddenRestore;
  button.disabled=true;
  if(stateless){const story=(data.hiddenStories||[]).find(item=>item.id===storyId);if(story){saveBrowserStory(story,{hidden:false});$('#status').textContent='Story restored. It appears again only if it still qualifies in the current feed.';}return;}
  try{await request(`/api/story/${storyId}`,{hidden:false});await load();$('#status').textContent=data.stories.some(story=>story.id===storyId)?'Story restored to the briefing.':'Story unhidden. It does not currently meet the briefing’s freshness and relevance rules.';}
  catch(err){button.disabled=false;$('#status').textContent=`Could not restore story: ${err.message}`;}
});
$('#deep-wrap').addEventListener('click',async e=>{
  if(e.target.id!=='complete-deep')return;
  e.target.disabled=true;
  if(stateless){const story=data.deepRead;if(story){const state=browserState(),date=localDateKey();saveBrowserStory(story,{read:true,read_date:date});if(!state.history.some(item=>item.id===story.id&&item.date===date))state.history.push({id:story.id,date,theme:data.focus,title:story.title,url:story.url,deep:true});writeBrowserState(state);applyBrowserState(data);render();$('#status').textContent='Today’s deep read added to your browser-only reading history.';}return;}
  try{await request('/api/reading/complete',{story_id:data.deepRead.id});await load();}
  catch(err){$('#status').textContent=`Could not save reading progress: ${err.message}`;e.target.disabled=false;}
});
async function refreshFeeds(initial=false) {
  const btn=$('#refresh');btn.disabled=true;btn.innerHTML='↻ <span>Checking feeds…</span>';$('#status').textContent='Fetching current stories from your enabled RSS sources…';
  try{const result=await request('/api/refresh',{});if(stateless){data=applyBrowserState(result);render();}else await load();$('#status').textContent=(result.refresh?.checked??result.checked)===0?'No verified sources are configured.':`Feed check complete · ${result.refresh?.new??result.new} stories · ${result.refresh?.grouped??result.grouped} grouped reports · ${result.refresh?.failed??result.failed} source${(result.refresh?.failed??result.failed)===1?'':'s'} failed. See source status for details.`;}
  catch(e){if(stateless&&data){data=applyBrowserState(data);render();}$('#status').textContent=`Could not refresh feeds: ${e.message}. Try again or check the configured source feeds.`;}
  btn.disabled=false;btn.innerHTML='↻ <span>Refresh feeds</span>';
}
$('#refresh').addEventListener('click',refreshFeeds);
$('#story-list').addEventListener('click',e=>{if(e.target.id==='empty-refresh')refreshFeeds();});
$('#sources-toggle').addEventListener('click',()=>{const panel=$('#source-panel'),expanded=panel.classList.contains('hidden');panel.classList.toggle('hidden');$('#sources-toggle').setAttribute('aria-expanded',String(expanded));});
load();
