/* MLB Game Totals Bet Cards — client logic.
   The model's total-runs PMF comes precomputed per game in data/slate-*.json.
   All odds math (price ANY entered total off the PMF -> de-vig sharp book ->
   blend with model -> edge -> quarter-Kelly) runs here so it updates live as
   you type, using the same params + distribution the Python engine produced. */

const state = { slate: null, params: null, bankroll: 1000, date: null,
                index: null, kellyFraction: 0.25, stakeMode: "kelly",
                flatUnit: 20, minEdge: 0.04 };

/* ---------- odds math (mirrors engine/model.py) ---------- */
const americanToProb = o => (o == null || (o > -100 && o < 100)) ? null : (o > 0 ? 100 / (o + 100) : (-o) / (-o + 100));
const americanToDecimal = o => (o == null || (o > -100 && o < 100)) ? null : 1 + (o > 0 ? o / 100 : 100 / -o);
function probToAmerican(p){
  if(!p || p <= 0 || p >= 1) return null;
  return p > 0.5 ? Math.round(-100 * p / (1 - p)) : Math.round(100 * (1 - p) / p);
}
function devigTwoWay(over, under){
  const po = americanToProb(over), pu = americanToProb(under);
  if(po == null || pu == null) return [null, null];
  const t = po + pu; if(t <= 0) return [null, null]; return [po / t, pu / t];
}
function blendWithMarket(pModel, pMarket, w){
  return pMarket == null ? pModel : w * pModel + (1 - w) * pMarket;
}
function kellyStake(prob, best, bankroll, P){
  const dec = americanToDecimal(best);
  if(dec == null || prob == null) return [0, 0];
  const b = dec - 1; if(b <= 0) return [0, 0];
  const full = (b * prob - (1 - prob)) / b;
  if(full <= 0) return [0, 0];
  const frac = Math.min(full * P.kelly_fraction, P.max_stake_frac);
  return [Math.round(bankroll * frac * 100) / 100, frac];
}
const fmtOdds = o => o == null ? "—" : (o > 0 ? "+" + o : "" + o);
const pct = x => (x * 100).toFixed(1) + "%";

/* ---------- total-runs distribution helpers (mirrors engine/model.py) ---------- */
/* Over wins on totals strictly greater than the line; Under on totals strictly
   less; a whole-number line pushes when the game lands exactly on it. */
function overUnderProbs(pmf, line){
  let pOver = 0, pUnder = 0, pPush = 0;
  for(let k = 0; k < pmf.length; k++){
    if(k > line) pOver += pmf[k];
    else if(k < line) pUnder += pmf[k];
    else pPush += pmf[k];          // k === line (whole-number line only)
  }
  return [pOver, pUnder, pPush];
}
/* Model Over prob conditioned on no push -> directly comparable to a de-vigged
   two-way market. */
function overProbNoPush(pOver, pUnder){
  const t = pOver + pUnder;
  return t > 0 ? pOver / t : null;
}

/* ---------- persistence ---------- */
const oddsKey = () => "mlbtot_odds_" + state.date;
function loadOdds(){ try { return JSON.parse(localStorage.getItem(oddsKey())) || {}; } catch { return {}; } }
function saveOdds(o){ localStorage.setItem(oddsKey(), JSON.stringify(o)); }
function loadBankroll(){ const v = parseFloat(localStorage.getItem("mlbtot_bankroll")); return isNaN(v) ? 1000 : v; }

/* ---------- parse typed values ---------- */
function parseOdds(str){
  if(str == null) return null;
  const s = ("" + str).trim().replace(/\s/g, "");
  if(s === "" || s === "+" || s === "-") return null;
  const n = Number(s);
  return isNaN(n) ? null : n;
}
function parseLine(str){
  if(str == null) return null;
  const s = ("" + str).trim();
  if(s === "") return null;
  const n = Number(s);
  return (isNaN(n) || n <= 0) ? null : n;
}

/* ---------- render ---------- */
function spLine(sp){
  const bits = [`RA9 <span class="v">${sp.ra9 ?? "—"}</span>`];
  if(sp.fip != null) bits.push(`FIP <span class="v">${sp.fip}</span>`);
  if(sp.xera != null) bits.push(`xERA <span class="v">${sp.xera}</span>`);
  return `<b>${sp.name}</b><span class="rp">${bits.join(" · ")}</span>`;
}

function buildCard(game, savedOdds){
  const tpl = document.getElementById("card-tpl").content.cloneNode(true);
  const root = tpl.querySelector(".card");
  const a = game.away, h = game.home, m = game.model;
  root.style.setProperty("--awayc", a.primary);
  root.style.setProperty("--homec", h.primary);
  root.dataset.pk = game.gamePk;

  const aw = root.querySelector(".team.away"), hm = root.querySelector(".team.home");
  aw.querySelector(".logo").src = a.logo; aw.querySelector(".abbr").textContent = a.abbr;
  aw.querySelector(".full").textContent = a.short;
  hm.querySelector(".logo").src = h.logo; hm.querySelector(".abbr").textContent = h.abbr;
  hm.querySelector(".full").textContent = h.short;

  const t = game.gameTime ? new Date(game.gameTime).toLocaleTimeString([], {hour:"numeric", minute:"2-digit"}) : "";
  root.querySelector(".gtime").textContent = t;

  root.querySelector(".away-sp").innerHTML = spLine(game.away_sp);
  root.querySelector(".home-sp").innerHTML = spLine(game.home_sp);

  root.querySelector(".projval").textContent = m.mu_total.toFixed(1);

  // total line: restore saved, else seed with the model projection (nearest .5)
  const lineEl = root.querySelector(".line-in");
  if(savedOdds && savedOdds.line != null && savedOdds.line !== ""){
    lineEl.value = savedOdds.line;
  } else {
    lineEl.placeholder = (Math.round(m.mu_total * 2) / 2).toFixed(1);
  }

  const fields = ["over-best","over-sharp","under-best","under-sharp"];
  fields.forEach(f => {
    const el = root.querySelector("." + f);
    if(savedOdds && savedOdds[f] != null) el.value = savedOdds[f];
    el.addEventListener("input", () => onOddsChange(game, root));
  });
  lineEl.addEventListener("input", () => onOddsChange(game, root));

  recompute(game, root);
  return root;
}

function onOddsChange(game, root){
  const all = loadOdds();
  const o = { line: root.querySelector(".line-in").value };
  ["over-best","over-sharp","under-best","under-sharp"].forEach(f => { o[f] = root.querySelector("." + f).value; });
  all[game.gamePk] = o;
  saveOdds(all);
  recompute(game, root);
}

/* reference (market) probability for a side, used as the edge benchmark.
   Priority: de-vig your two Best prices -> single Best price implied ->
   de-vig the Sharp prices. So an edge appears as soon as you enter a line + a
   Best price, with no sharp needed. */
function refProb(bestSame, bestOther, sharpNov){
  const [dh] = devigTwoWay(bestSame, bestOther);
  if(dh != null) return dh;                 // both best sides -> clean no-vig
  const imp = americanToProb(bestSame);
  if(imp != null) return imp;               // single best price -> raw implied
  return sharpNov;                          // fall back to sharp no-vig
}

/* Bet score 0-100: edge size (0-55) + model conviction (0-25) + sharp
   confirmation (±20). Without a sharp price it tops out ~80; a sharp that
   agrees pushes toward 100, one that disagrees drags it down. */
function betScore(p, edge, refP, sharpNov, sharpPresent){
  if(p == null || edge == null) return null;
  const eC = Math.max(0, Math.min(edge / 0.08, 1));          // 8% edge = full
  const vC = Math.max(0, Math.min((p - 0.5) / 0.12, 1));     // model conviction
  let score = 100 * (0.55 * eC + 0.25 * vC);
  if(sharpPresent && sharpNov != null && refP != null){
    const agree = Math.max(-1, Math.min((sharpNov - refP) / 0.04, 1));
    score += 20 * agree;
  }
  return Math.max(0, Math.min(Math.round(score), 100));
}
function scoreBand(s){
  if(s == null) return ["", ""];
  if(s >= 75) return ["MAX", "max"];
  if(s >= 60) return ["STRONG", "strong"];
  if(s >= 40) return ["LEAN", "lean"];
  return ["WEAK", "weak"];
}

function recompute(game, root){
  const P = Object.assign({}, state.params, {kelly_fraction: state.kellyFraction});
  const BR = state.bankroll;
  const lineEl = root.querySelector(".line-in");
  const line = parseLine(lineEl.value) ?? parseLine(lineEl.placeholder);

  const oBest  = parseOdds(root.querySelector(".over-best").value);
  const oSharp = parseOdds(root.querySelector(".over-sharp").value);
  const uBest  = parseOdds(root.querySelector(".under-best").value);
  const uSharp = parseOdds(root.querySelector(".under-sharp").value);

  // model probabilities at this line
  const [pOver, pUnder, pPush] = overUnderProbs(game.pmf, line);
  const modelOver = overProbNoPush(pOver, pUnder);
  const modelUnder = modelOver == null ? null : 1 - modelOver;

  // probability bar + headline
  root.querySelector(".over-fill").style.width = ((modelOver ?? 0.5) * 100) + "%";
  root.querySelector(".under-fill").style.width = ((modelUnder ?? 0.5) * 100) + "%";
  root.querySelector(".overp").textContent = `O ${pct(modelOver ?? 0.5)}`;
  root.querySelector(".underp").textContent = `${pct(modelUnder ?? 0.5)} U`;
  let env = `proj ${game.model.er_away}–${game.model.er_home} · PF ${game.park_factor}`;
  if(pPush > 0.001) env += ` · push ${pct(pPush)}`;
  if(game.weather && game.weather.applied && game.weather.temp_f != null)
    env += ` · ${Math.round(game.weather.temp_f)}°`;
  root.querySelector(".env").textContent = env;

  // sharp-book de-vig + blend (optional; tempers model overconfidence)
  const [oSharpNov] = devigTwoWay(oSharp, uSharp);
  const uSharpNov = oSharpNov == null ? null : 1 - oSharpNov;
  const sharpPresent = oSharpNov != null;
  const pOverFinal  = modelOver == null ? null
                    : (sharpPresent ? blendWithMarket(modelOver, oSharpNov, P.market_blend) : modelOver);
  const pUnderFinal = pOverFinal == null ? null : 1 - pOverFinal;

  const sides = [
    {key:"over",  p:pOverFinal,  best:oBest, other:uBest, sharpNov:oSharpNov,
     label:"Over " + (line ?? "?"),
     fairEl:root.querySelector(".over-fair"),  edgeEl:root.querySelector(".over-edge")},
    {key:"under", p:pUnderFinal, best:uBest, other:oBest, sharpNov:uSharpNov,
     label:"Under " + (line ?? "?"),
     fairEl:root.querySelector(".under-fair"), edgeEl:root.querySelector(".under-edge")},
  ];

  let bestPlay = null, scoreSide = null;
  for(const s of sides){
    s.fairEl.textContent = fmtOdds(probToAmerican(s.p));
    s.refP = refProb(s.best, s.other, s.sharpNov);
    s.edge = (s.refP == null || s.p == null) ? null : s.p - s.refP;
    const el = s.edgeEl;
    el.classList.remove("pos", "neg");
    if(s.edge == null){ el.textContent = "—"; }
    else {
      el.textContent = (s.edge >= 0 ? "+" : "") + (s.edge * 100).toFixed(1) + "%";
      el.classList.add(s.edge > 0 ? "pos" : "neg");
    }
    // track the most favorable side for the bet score
    if(s.edge != null && (scoreSide == null || s.edge > scoreSide.edge)) scoreSide = s;
    // a play requires a real price to bet (Best) + qualifying edge
    if(s.edge != null && s.edge >= state.minEdge && s.best != null){
      let stake, frac;
      if(state.stakeMode === "flat"){
        stake = Math.min(state.flatUnit, BR);
        frac = BR > 0 ? stake / BR : 0;
      } else {
        [stake, frac] = kellyStake(s.p, s.best, BR, P);
      }
      if(stake > 0 && (!bestPlay || s.edge > bestPlay.side.edge))
        bestPlay = {side:s, stake, frac};
    }
  }

  // ---- bet score chip ----
  const score = scoreSide ? betScore(scoreSide.p, scoreSide.edge, scoreSide.refP,
                                     scoreSide.sharpNov, sharpPresent) : null;
  const [bsLab, bsCls] = scoreBand(score);
  const bsEl = root.querySelector(".betscore");
  bsEl.className = "betscore " + (bsCls || "none");
  bsEl.querySelector(".bs-num").textContent = score == null ? "—" : score;
  bsEl.querySelector(".bs-lab").textContent = bsLab;

  const rec = root.querySelector(".rec");
  if(bestPlay){
    const s = bestPlay.side;
    const units = (bestPlay.stake / (BR * 0.01)).toFixed(1);
    const anchor = sharpPresent ? "" : " · raw model (no sharp)";
    rec.className = "rec play " + s.key;
    rec.innerHTML = `<span class="tag">Bet</span>
      <span><b>${s.label}</b> ${fmtOdds(s.best)} —
      <span class="stake">$${bestPlay.stake.toFixed(2)}</span>
      <span class="units">(${units}u · ${pct(s.p)} · edge +${(s.edge*100).toFixed(1)}%${anchor})</span></span>`;
    root.classList.add("is-bet");
  } else {
    rec.className = "rec";
    root.classList.remove("is-bet");
    const fairO = probToAmerican(pOverFinal), fairU = probToAmerican(pUnderFinal);
    const haveBest = oBest != null || uBest != null;
    rec.innerHTML = haveBest
      ? `<span>No qualifying edge (need ≥ ${(state.minEdge*100).toFixed(0)}%).
         Fair: O ${fmtOdds(fairO)} / U ${fmtOdds(fairU)} @ ${line ?? "?"}</span>`
      : `<span>Fair: Over ${fmtOdds(fairO)} / Under ${fmtOdds(fairU)} @ ${line ?? "?"}.
         Enter a Best price to see your edge. Sharp is optional (it tempers the model).</span>`;
  }
}

const KELLY_LABEL = {"1":"Full", "0.5":"½", "0.25":"¼", "0.125":"⅛"};

async function loadSlate(dateStr){
  const wrap = document.getElementById("cards");
  wrap.innerHTML = `<div class="empty">Loading ${dateStr}…</div>`;
  try {
    const res = await fetch(`data/slate-${dateStr}.json?` + Date.now());
    if(!res.ok) throw new Error(res.status);
    state.slate = await res.json();
  } catch(e){
    wrap.innerHTML = `<div class="empty">Couldn't load the slate for ${dateStr}.<br>${e}</div>`;
    return;
  }
  state.params = state.slate.params;
  state.date = state.slate.date;
  document.getElementById("date-select").value = dateStr;
  renderHeader();
  renderAll();
}

function renderHeader(){
  const d = new Date(state.date + "T12:00:00");
  const isToday = state.index && state.date === state.index.today;
  document.getElementById("slate-date").textContent =
    (isToday ? "Today · " : "") +
    d.toLocaleDateString([], {weekday:"long", month:"long", day:"numeric", year:"numeric"});
  const gen = new Date(state.slate.generated_at);
  document.getElementById("meta").innerHTML =
    `${state.slate.games.length} games · ratings ${state.slate.season}<br>updated ${gen.toLocaleString()}`;
  const stakeDesc = state.stakeMode === "flat"
    ? `flat $${state.flatUnit}/play`
    : `${KELLY_LABEL[String(state.kellyFraction)] || state.kellyFraction+"×"}-Kelly capped ${(state.params.max_stake_frac*100).toFixed(0)}%`;
  document.getElementById("footnote").innerHTML =
    `Edge = model fair prob − your Best-book price (shows with just a line + Best price). Sharp is optional: when entered it blends ${Math.round(state.params.market_blend*100)}/${Math.round((1-state.params.market_blend)*100)} to temper the model and confirm the play ·
     Bet score 0–100 (edge + conviction + sharp) · bets flagged at ≥ ${(state.minEdge*100).toFixed(0)}% edge · ${stakeDesc} · entries saved on this device.`;
}

function stepDate(delta){
  const dates = state.index.dates.map(x => x.date);
  const i = dates.indexOf(state.date);
  const j = i + delta;
  if(j >= 0 && j < dates.length) loadSlate(dates[j]);
}

/* ---------- boot ---------- */
async function init(){
  const brInput = document.getElementById("bankroll");
  state.bankroll = loadBankroll();
  brInput.value = state.bankroll;
  brInput.addEventListener("input", () => {
    const v = parseFloat(brInput.value); state.bankroll = isNaN(v) ? 0 : v;
    localStorage.setItem("mlbtot_bankroll", state.bankroll);
    renderAll();
  });

  const flatWrap = document.getElementById("flatunit-wrap");
  const flatInput = document.getElementById("flat-unit");
  const sSel = document.getElementById("stake-select");
  const savedStake = localStorage.getItem("mlbtot_stake") || "0.25";
  const fSaved = parseFloat(localStorage.getItem("mlbtot_flat_unit"));
  state.flatUnit = isNaN(fSaved) ? 20 : fSaved;
  flatInput.value = state.flatUnit;

  function applyStake(val){
    if(val === "flat"){ state.stakeMode = "flat"; flatWrap.hidden = false; }
    else { state.stakeMode = "kelly"; state.kellyFraction = parseFloat(val); flatWrap.hidden = true; }
  }
  sSel.value = savedStake;
  applyStake(savedStake);
  sSel.addEventListener("change", () => {
    localStorage.setItem("mlbtot_stake", sSel.value);
    applyStake(sSel.value);
    renderHeader(); renderAll();
  });
  flatInput.addEventListener("input", () => {
    const v = parseFloat(flatInput.value); state.flatUnit = isNaN(v) ? 0 : v;
    localStorage.setItem("mlbtot_flat_unit", state.flatUnit);
    renderHeader(); renderAll();
  });

  const eSel = document.getElementById("edge-select");
  const eSaved = parseFloat(localStorage.getItem("mlbtot_minedge"));
  state.minEdge = isNaN(eSaved) ? 0.04 : eSaved;
  eSel.value = String(state.minEdge);
  eSel.addEventListener("change", () => {
    state.minEdge = parseFloat(eSel.value);
    localStorage.setItem("mlbtot_minedge", state.minEdge);
    renderHeader(); renderAll();
  });

  try {
    const res = await fetch("data/index.json?" + Date.now());
    state.index = await res.json();
  } catch(e){
    document.getElementById("cards").innerHTML =
      `<div class="empty">Couldn't load the schedule index.<br>${e}</div>`;
    return;
  }
  const dSel = document.getElementById("date-select");
  dSel.innerHTML = "";
  for(const d of state.index.dates){
    const o = document.createElement("option");
    const dd = new Date(d.date + "T12:00:00");
    o.value = d.date;
    o.textContent = (d.date === state.index.today ? "Today · " : "") +
      dd.toLocaleDateString([], {weekday:"short", month:"short", day:"numeric"}) +
      `  (${d.games})`;
    dSel.appendChild(o);
  }
  dSel.addEventListener("change", () => loadSlate(dSel.value));
  document.getElementById("date-prev").addEventListener("click", () => stepDate(-1));
  document.getElementById("date-next").addEventListener("click", () => stepDate(1));

  await loadSlate(state.index.today);
}

function renderAll(){
  if(!state.slate) return;
  const wrap = document.getElementById("cards");
  wrap.innerHTML = "";
  const saved = loadOdds();
  if(!state.slate.games.length){
    wrap.innerHTML = `<div class="empty">No games scheduled for this date.</div>`; return;
  }
  for(const g of state.slate.games) wrap.appendChild(buildCard(g, saved[g.gamePk]));
}

init();
