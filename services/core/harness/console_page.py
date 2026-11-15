"""The console's single page. No external assets; all dynamic text is set with textContent."""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Switchboard · live demo console</title>
<style>
  :root { color-scheme: dark; font-family: system-ui, sans-serif; background: #171a17;
    color: #f5f2e7; --muted: #c1c8b9; --accent: #dffa9b; --panel: #222721; --line: #515c4d; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 1rem; }
  header, main { max-width: 84rem; margin: 0 auto; }
  h1 { font-family: Georgia, serif; font-weight: 400; font-size: clamp(1.8rem, 4vw, 2.6rem);
    margin: .2rem 0 .5rem; letter-spacing: -.02em; }
  h2 { font-size: .8rem; color: var(--accent); text-transform: uppercase; letter-spacing: .11em;
    margin: 0 0 .8rem; }
  .notice { border-left: 2px solid var(--accent); padding-left: 1rem; color: var(--muted);
    max-width: 62rem; line-height: 1.5; margin: 0 0 1rem; }
  main { display: grid; grid-template-columns: minmax(16rem, 1fr) minmax(0, 1.7fr) minmax(16rem, 1fr);
    gap: 1rem; align-items: start; }
  section { background: var(--panel); border: 1px solid var(--line); border-radius: 1rem;
    padding: 1.1rem; min-width: 0; }
  button { font: inherit; cursor: pointer; border-radius: .7rem; border: 1px solid var(--line);
    background: #2c332a; color: inherit; padding: .6rem .8rem; }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px; }
  button:disabled { opacity: .5; cursor: progress; }
  .scenario { display: block; width: 100%; text-align: left; margin-bottom: .5rem; }
  .scenario span { display: block; color: var(--muted); font-size: .85rem; margin-top: .2rem; }
  label { display: block; font-size: .85rem; color: var(--muted); margin: .7rem 0 .25rem; }
  input, textarea, select { width: 100%; font: inherit; color: inherit; background: #171a17;
    border: 1px solid var(--line); border-radius: .6rem; padding: .55rem .65rem; }
  textarea { min-height: 5.5rem; resize: vertical; }
  .primary { background: var(--accent); color: #21291a; font-weight: 700; border-color: var(--accent);
    margin-top: .8rem; }
  .hint { color: var(--muted); font-size: .85rem; line-height: 1.45; }
  #status { min-height: 1.5rem; margin: 0 0 .8rem; color: var(--accent); }
  #status.error { color: #ffb4a2; }
  .case, .review { border: 1px solid var(--line); border-radius: .8rem; padding: .9rem;
    margin-bottom: .8rem; overflow-wrap: anywhere; }
  .case small, .review small { color: var(--muted); }
  blockquote { margin: .5rem 0; background: #343e32; padding: .7rem .9rem;
    border-radius: .8rem .8rem .8rem .2rem; white-space: pre-wrap; line-height: 1.5; }
  blockquote.reply { background: #2b3440; border-radius: .8rem .8rem .2rem .8rem; }
  .badges { display: flex; flex-wrap: wrap; gap: .4rem; margin: .5rem 0; }
  .badge { background: var(--accent); color: #21291a; border-radius: 2rem; padding: .25rem .7rem;
    font-size: .82rem; font-weight: 700; }
  details { margin-top: .5rem; }
  summary { cursor: pointer; color: var(--muted); }
  ol { padding-left: 1.2rem; margin: .5rem 0 0; }
  li { margin: .3rem 0; font-size: .88rem; line-height: 1.45; }
  .row { display: flex; gap: .5rem; margin-top: .6rem; }
  .row button { flex: 1; }
  .approve { background: var(--accent); color: #21291a; font-weight: 700; }
  .empty { color: var(--muted); font-style: italic; }
  @media (max-width: 64rem) { main { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>Switchboard live demo console</h1>
  <p class="notice">This is a live run of the <b>mock stack</b> for the fictional retailer
  Northwind Apparel. The AI model is a deterministic stand-in, the shop and Slack are local
  fixtures, and no real email is sent and no real money moves. Each step runs a real n8n
  workflow against the real policy engine, so a step can take up to a minute.</p>
  <p id="status" role="status" aria-live="polite"></p>
</header>
<main>
  <section aria-labelledby="h-send">
    <h2 id="h-send">1 · Send a customer email</h2>
    <div id="scenarios">__SCENARIOS__</div>
    <form id="free">
      <label for="sender">Or write your own. From</label>
      <select id="sender">
        <option value="emma.jones@example.com">Emma Jones (owns NW-1001, NW-1007)</option>
        <option value="liam.chen@example.com">Liam Chen (owns NW-1002)</option>
        <option value="sofia.rossi@example.com">Sofia Rossi (owns NW-1003, NW-1008)</option>
        <option value="arjun.mehta@example.com">Arjun Mehta (owns NW-1004)</option>
        <option value="random.person@example.com">Someone with no orders</option>
      </select>
      <label for="text">Message</label>
      <textarea id="text" maxlength="2000" required></textarea>
      <button class="primary" type="submit">Send message</button>
      <p class="hint">The stand-in model only recognises the scripted messages and
      "where … NW-number" questions. Anything else is classified as "other" and goes to a
      reviewer. That is the stand-in's limit, not a measure of a real model.</p>
    </form>
  </section>
  <section aria-labelledby="h-cases">
    <h2 id="h-cases">2 · What the system did</h2>
    <div id="cases"><p class="empty">Nothing sent yet.</p></div>
  </section>
  <section aria-labelledby="h-reviews">
    <h2 id="h-reviews">3 · You are the reviewer</h2>
    <div id="reviews"><p class="empty">Nothing is waiting for you.</p></div>
    <p class="hint">Your click is signed as the named fixture staff member and checked by the
    database against the role the policy requires.</p>
  </section>
</main>
<script>
const TOKEN = "__TOKEN__";
const FIELDS = {
  message: ["body_redacted"], classification: ["intent", "confidence", "flags"],
  lookup: ["tool_key", "status", "result_reason"],
  policy_decision: ["rule_id", "rule_source", "decision"], draft: ["status"],
  action: ["tool_key", "risk_level", "status", "amount", "currency"],
  approval: ["required_role", "decision"], audit: ["audit_event", "audit_sequence"]
};
const status = document.getElementById("status");
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
function setBusy(busy, message) {
  document.querySelectorAll("button").forEach(button => { button.disabled = busy; });
  status.className = "";
  status.textContent = message || "";
}
function fail(message) { status.className = "error"; status.textContent = message; }
async function call(path, body, working) {
  setBusy(true, working);
  try {
    const response = await fetch(path, {
      method: body ? "POST" : "GET",
      headers: {"X-Console-Token": TOKEN, "Content-Type": "application/json"},
      body: body ? JSON.stringify(body) : undefined
    });
    const data = await response.json();
    if (!response.ok) {
      setBusy(false);
      fail(typeof data.detail === "string" ? data.detail : "The request was not accepted.");
      return;
    }
    render(data);
    setBusy(false, body ? "Done." : "");
  } catch (error) {
    setBusy(false);
    fail("The console server did not answer. Is scripts/demo_live still running?");
  }
}
function timeline(rows) {
  const details = el("details");
  details.append(el("summary", "", "Technical evidence (" + rows.length + " events)"));
  const list = el("ol");
  for (const row of rows) {
    const item = el("li");
    item.append(el("b", "", String(row.event_kind).replaceAll("_", " ")));
    const parts = (FIELDS[row.event_kind] || [])
      .filter(key => row[key] !== null && row[key] !== undefined && String(row[key]) !== "")
      .map(key => key.replaceAll("_", " ") + " " + row[key]);
    item.append(document.createTextNode(parts.length ? " · " + parts.join(" · ") : ""));
    list.append(item);
  }
  details.append(list);
  return details;
}
function render(data) {
  const cases = document.getElementById("cases");
  cases.replaceChildren();
  if (!data.cases.length) cases.append(el("p", "empty", "Nothing sent yet."));
  for (const item of data.cases) {
    const card = el("article", "case");
    card.append(el("small", "", item.label + " · from " + item.sender));
    card.append(el("blockquote", "", item.text));
    const badges = el("div", "badges");
    for (const badge of item.badges) badges.append(el("span", "badge", badge));
    if (!item.badges.length) badges.append(el("small", "", "No outcome recorded yet."));
    card.append(badges);
    if (item.reply) {
      card.append(el("small", "", item.reply_status === "sent"
        ? "Reply sent (mock outbound)"
        : "Draft, not sent (status: " + item.reply_status + ")"));
      card.append(el("blockquote", "reply", item.reply));
    }
    card.append(timeline(item.timeline));
    cases.append(card);
  }
  const reviews = document.getElementById("reviews");
  reviews.replaceChildren();
  if (!data.reviews.length) reviews.append(el("p", "empty", "Nothing is waiting for you."));
  for (const review of data.reviews) {
    const card = el("article", "review");
    const what = review.tool_key
      ? review.tool_key + (review.amount ? " · " + review.currency + " " + review.amount : "")
      : "Reply draft";
    card.append(el("b", "", what));
    card.append(el("p", "", review.reason));
    card.append(el("small", "", "Needs role: " + review.required_role +
      ". You sign as " + review.signs_as + "."));
    const row = el("div", "row");
    for (const [decision, label, cls] of [["approve", "Approve", "approve"], ["reject", "Reject", ""]]) {
      const button = el("button", cls, label);
      button.addEventListener("click", () => call("/api/reviews/" + review.id, {decision},
        "Recording your decision and dispatching (about 20 seconds)…"));
      row.append(button);
    }
    card.append(row);
    reviews.append(card);
  }
}
const WORKING = "Running the n8n pipeline (up to a minute)…";
document.querySelectorAll(".scenario").forEach(button => {
  button.addEventListener("click", () => call("/api/send", {scenario: button.dataset.key}, WORKING));
});
document.getElementById("free").addEventListener("submit", event => {
  event.preventDefault();
  const text = document.getElementById("text").value.trim();
  if (!text) return;
  call("/api/send", {sender: document.getElementById("sender").value, text}, WORKING);
});
call("/api/state");
</script>
</body>
</html>
"""
