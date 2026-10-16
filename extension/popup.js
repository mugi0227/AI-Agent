"use strict";
const $ = (id) => document.getElementById(id);
let preview = null;
const status = (message) => { $("status").textContent = message; };
const hosts = () => $("hosts").value.split(",").map(x => x.trim().toLowerCase()).filter(Boolean);
const reset = () => { preview = null; $("send").disabled = true; };
chrome.storage.local.get({allowedHosts: []}).then(x => { $("hosts").value = x.allowedHosts.join(", "); });
$("save").addEventListener("click", async () => {
  const values = hosts();
  if (values.some(x => !/^[a-z0-9.-]+$/.test(x))) return status("ホスト名だけを完全一致で指定してください。");
  await chrome.storage.local.set({allowedHosts: values}); reset(); status("保存しました。本体の許可リストにも登録が必要です。");
});
$("hosts").addEventListener("input", reset);
$("scope").addEventListener("change", reset);
$("ping").addEventListener("click", async () => {
  try { const reply = await chrome.runtime.sendNativeMessage("com.workassistant.capture", {operation: "ping"}); status(JSON.stringify(reply)); }
  catch (_) { status("接続できません。会社PC検証手順のNative Messaging項目を確認してください。"); }
});
$("preview").addEventListener("click", async () => {
  reset();
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    const url = new URL(tab.url);
    if (url.protocol !== "https:" || !hosts().includes(url.hostname) || url.username || url.password || (url.port && url.port !== "443")) throw new Error("HOST_BLOCKED");
    const scope = $("scope").value;
    const [{result}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, args: [scope], func: (mode) => {
      const selection = window.getSelection();
      const sensitive = "input,textarea,[contenteditable='true'],[role='textbox'],[type='password']";
      if (mode === "selection") {
        const anchor = selection && (selection.anchorNode?.parentElement);
        if (document.activeElement?.matches(sensitive) || anchor?.closest(sensitive)) return {text: "", title: document.title};
        return {text: (selection?.toString() || "").slice(0, 60000), title: document.title};
      }
      const chunks = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let node, length = 0;
      while ((node = walker.nextNode()) && length < 60000) {
        const element = node.parentElement;
        if (!element || element.closest("script,style,noscript,template,form," + sensitive)) continue;
        const style = getComputedStyle(element);
        if (style.visibility === "hidden" || style.display === "none" || !element.getClientRects().length) continue;
        const text = node.textContent.trim();
        if (text) { chunks.push(text); length += text.length + 1; }
      }
      return {text: chunks.join("\n").slice(0, 60000), title: document.title};
    }});
    url.search = ""; url.hash = "";
    if (!result.text.trim()) return status("対象の文章がありません。本文を選択してください。入力欄は対象外です。");
    preview = {operation: "capture", url: url.href, title: result.title.slice(0, 1000) || "ページ", scope};
    $("text").value = result.text;
    $("source").textContent = url.href + " / " + scope + "（添付資料・全履歴を含むとは限りません）";
    $("send").disabled = false; status("内容を確認してから送信してください。Bedrockへはここから送信しません。");
  } catch (_) { status("このページは取得できません。許可ホスト・閲覧範囲・管理ポリシーを確認してください。"); }
});
$("send").addEventListener("click", async () => {
  if (!preview || !$("text").value.trim()) return;
  $("send").disabled = true;
  try {
    const reply = await chrome.runtime.sendNativeMessage("com.workassistant.capture", {...preview, text: $("text").value});
    status(reply.message || reply.error || reply.status);
  } catch (_) { status("保存できません。Native hostの登録と許可設定を確認してください。"); }
  finally { $("send").disabled = !preview; }
});
