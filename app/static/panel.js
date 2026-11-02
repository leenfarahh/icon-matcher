// The Prezlab Icons panel inside Claude: the bridge between the page (index.html) and Claude. connector.py inlines
// it right after the MCP Apps client, in the same function, so App is in scope. The page waits on HOST.ready; this
// learns the run's private address from start_icon_run's result, and from then on the page works as it does locally.
// Downloads go through Claude (a panel can't save files itself), and Claude hears what the designer did here.
const PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation";
const app = new App({name: "Prezlab Icons", version: "1.0.0"}, {availableDisplayModes: ["inline", "fullscreen"]});
let link = "";   // the same page at the run's private link, for "Open in browser"

app.ontoolresult = result => {
  const s = result.structuredContent || {};
  if (HOST.base || typeof s.base !== "string") return;
  HOST.base = s.base;
  link = s.link || "";
  browser.hidden = !link;
  HOST.resolve();
};

async function tell(text) {   // Claude reads this with the designer's next message
  try {
    await app.updateModelContext({content: [{type: "text", text}]});
  } catch (e) {
    console.warn("[Prezlab Icons] couldn't update Claude", e);
  }
}

HOST.ran = r => {
  const open = r.flags.filter(f => f.spot).length;
  tell([`The designer ran a deck in the Prezlab Icons panel (run ${r.job}, mode ${r.mode}): ${r.picks.length} icons `
        + `${r.mode === "suggest" ? "suggested" : "placed"}, ${r.flags.length} items flagged`
        + (open ? `, ${open} of them placeholders they can fill in the panel.` : "."),
        r.output_file ? "The copy with icons is ready to download in the panel."
                      : "Nothing is in the deck until they add their picks in the panel.",
        "Call get_icon_run for the details."].join(" "));
};

HOST.applied = b => tell(`The designer added ${b.added} icons in the Prezlab Icons panel; the new copy `
                         + `(${b.output_file}) is ready to download there.`);

HOST.download = async url => {
  if (app.getHostCapabilities()?.downloadFile) {
    try {
      const out = await app.downloadFile({contents: [
        {type: "resource_link", uri: url, name: HOST.output || "deck_icons.pptx", mimeType: PPTX}]});
      if (!out.isError) return;
    } catch (e) {
      console.warn("[Prezlab Icons] Claude couldn't save the file", e);
    }
  }
  await app.openLink({url});   // the browser downloads it
};

// Full screen, where Claude offers it (an inline panel is capped in height), and the same page in the browser.
const nav = document.querySelector("header nav");
const display = Object.assign(document.createElement("button"), {type: "button", hidden: true});
const browser = Object.assign(document.createElement("button"), {type: "button", hidden: true,
                                                                  textContent: "Open in browser"});
nav.append(display, browser);

function drawDisplay() {
  const context = app.getHostContext() || {};
  const inline = (context.displayMode || "inline") === "inline";
  display.hidden = inline && !(context.availableDisplayModes || []).includes("fullscreen");
  display.textContent = inline ? "Full screen" : "Back to the chat";
}

display.onclick = async () => {
  const inline = (app.getHostContext()?.displayMode || "inline") === "inline";
  try {
    await app.requestDisplayMode({mode: inline ? "fullscreen" : "inline"});
  } catch (e) {
    console.warn("[Prezlab Icons] display mode refused", e);
  }
  drawDisplay();
};
browser.onclick = () => { if (link) app.openLink({url: link}); };
app.onhostcontextchanged = drawDisplay;

await app.connect();
drawDisplay();
