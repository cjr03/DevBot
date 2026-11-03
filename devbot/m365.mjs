// Teams chats and Outlook mail for the dashboard, read with the signed-in Teams web session in DevBot's Chrome
// (port from argv[2]). Python's stdlib has no WebSocket to reach Chrome, hence Node. Prints {"teams": [...], "outlook": [...]}
// in the dashboard's message format; tokens never leave this process.
const [port, owner, days = 7] = process.argv.slice(2);
const since = Date.now() - days * 864e5;

async function tokens() {
  const find = async () => (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find(p => p.type === "page" && /^https:\/\/teams\.(microsoft|cloud\.microsoft)\.com$|^https:\/\/teams\.cloud\.microsoft$/.test(new URL(p.url).origin));
  let page = await find();
  if (!page) {  // model runs drive the shared tab elsewhere: open Teams in a tab of our own and let it sign in from cookies
    await fetch(`http://127.0.0.1:${port}/json/new?https://teams.cloud.microsoft/`, { method: "PUT" });
    for (let i = 0; i < 20 && !page; i++) { await new Promise(r => setTimeout(r, 1500)); page = await find(); }
    await new Promise(r => setTimeout(r, 8000));  // tokens land in localStorage a few seconds after load
  }
  if (!page) throw new Error("Teams isn't open in DevBot's browser");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((ok, bad) => { ws.onopen = ok; ws.onerror = bad; });
  const value = await new Promise(ok => {
    ws.onmessage = m => ok(JSON.parse(m.data).result?.result?.value);
    ws.send(JSON.stringify({ id: 1, method: "Runtime.evaluate", params: { returnByValue: true, expression: `(() => {
      const o = {};
      for (const k of Object.keys(localStorage)) {
        if (!/accesstoken/i.test(k)) continue;
        try { const v = JSON.parse(localStorage[k]);
          if (v.target.includes("graph.microsoft.com/Mail.Read")) o.graph = v.secret;
          if (v.target.startsWith("https://api.spaces.skype.com")) o.spaces = v.secret; } catch (e) {}
      }
      return o; })()` } }));
  });
  ws.close();
  if (!value?.graph || !value?.spaces) throw new Error("Teams web is signed out");
  return value;
}

const text = html => String(html || "").replace(/<blockquote[^>]*schema\.skype\.com\/Reply[\s\S]*?<\/blockquote>/gi, "")  // a reply's quote of the earlier message
  .replace(/<br\s*\/?>|<\/p>|<\/div>/gi, "\n").replace(/<[^>]+>/g, "")
  .replace(/&nbsp;/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&").trim();

async function teams(spaces) {
  const auth = await (await fetch("https://teams.microsoft.com/api/authsvc/v1.0/authz", { method: "POST", headers: { Authorization: `Bearer ${spaces}` } })).json();
  const base = auth.regionGtms.chatService, h = { headers: { Authentication: `skypetoken=${auth.tokens.skypeToken}` } };
  const convs = (await (await fetch(`${base}/v1/users/ME/conversations?view=msnp24Equivalent&pageSize=100`, h)).json()).conversations || [];
  const out = [];
  for (const c of convs) {
    if (!c.id.startsWith("19:") || Date.parse(c.lastMessage?.originalarrivaltime || 0) < since) continue;
    const msgs = (await (await fetch(`${base}/v1/users/ME/conversations/${encodeURIComponent(c.id)}/messages?view=msnp24Equivalent&pageSize=200`, h)).json()).messages || [];
    const thread = msgs.filter(m => /^(Text|RichText)/.test(m.messagetype) && m.imdisplayname && text(m.content))
      .map(m => ({ from: m.imdisplayname, at: m.originalarrivaltime, text: text(m.content) })).reverse();
    if (!thread.length) continue;
    const last = thread[thread.length - 1];
    const other = thread.find(t => t.from !== owner && !t.from.includes(owner.split(" ").reverse().join(", ")));
    out.push({ id: `teams-${c.id}-${last.at}`, source: "Teams", chat: c.threadProperties?.topic || other?.from || "Teams chat",
               from: last.from, at: last.at, text: last.text, reply: "", thread });
  }
  return out;
}

async function outlook(graph) {
  const url = `https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$top=100&$orderby=receivedDateTime desc` +
    `&$filter=receivedDateTime ge ${new Date(since).toISOString()}&$select=id,subject,from,receivedDateTime,bodyPreview`;
  const r = await fetch(url, { headers: { Authorization: `Bearer ${graph}` } });
  if (!r.ok) throw new Error(`Graph mail ${r.status}`);
  return ((await r.json()).value || []).map(m => ({ id: m.id, source: "Outlook", chat: m.subject || "(no subject)",
    from: m.from?.emailAddress?.name || m.from?.emailAddress?.address || "?", at: m.receivedDateTime, text: m.bodyPreview || "", reply: "", thread: [] }));
}

const t = await tokens().catch(e => { console.log(JSON.stringify({ errors: [String(e.message || e)] })); process.exit(1); });
const [tm, ol] = await Promise.allSettled([teams(t.spaces), outlook(t.graph)]);
console.log(JSON.stringify({ teams: tm.value || [], outlook: ol.value || [],
  errors: [tm, ol].filter(x => x.status === "rejected").map(x => String(x.reason?.message || x.reason)) }));
