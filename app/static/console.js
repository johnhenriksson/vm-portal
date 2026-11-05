// VM console page: noVNC connected to the portal's console relay.
import RFB from "./novnc/core/rfb.js";

const screen = document.getElementById("screen");
const status = document.getElementById("console-status");
const btn = (a) => document.querySelector(`[data-action=${a}]`);
let rfb = null;

function setStatus(text, cls) {
  status.textContent = text;
  status.className = "badge " + cls;
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  rfb = new RFB(screen, `${proto}//${location.host}${screen.dataset.ws}`, { wsProtocols: ["binary"] });
  rfb.scaleViewport = btn("scale").getAttribute("aria-pressed") === "true";
  rfb.focusOnClick = true;
  setStatus("Connecting", "queued");
  btn("reconnect").hidden = true;
  rfb.addEventListener("connect", () => { setStatus("Connected", "ready"); rfb.focus(); });
  rfb.addEventListener("disconnect", (e) => {
    setStatus(e.detail.clean ? "Disconnected" : "Connection lost", "failed");
    btn("reconnect").hidden = false;
  });
  rfb.addEventListener("securityfailure", (e) => setStatus("Refused: " + (e.detail.reason || "security"), "failed"));
}

btn("cad").addEventListener("click", () => rfb && rfb.sendCtrlAltDel());
btn("reconnect").addEventListener("click", connect);
btn("scale").addEventListener("click", (e) => {
  const on = e.currentTarget.getAttribute("aria-pressed") !== "true";
  e.currentTarget.setAttribute("aria-pressed", String(on));
  if (rfb) rfb.scaleViewport = on;
});
btn("fullscreen").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else screen.requestFullscreen();
});

// "Paste" by typing: X11 keysyms are the code point for Latin-1, 0x01000000+cp beyond that.
const dialog = document.getElementById("paste-dialog");
btn("paste").addEventListener("click", () => dialog.showModal());
dialog.addEventListener("close", async () => {
  const text = document.getElementById("paste-text").value;
  if (dialog.returnValue !== "type" || !rfb || !text) return;
  for (const ch of text) {
    const cp = ch.codePointAt(0);
    const keysym = ch === "\n" ? 0xff0d : ch === "\t" ? 0xff09 : cp < 0x100 ? cp : 0x01000000 + cp;
    rfb.sendKey(keysym, null);
    await new Promise((r) => setTimeout(r, 8));  // don't overrun the guest's keyboard buffer
  }
  rfb.focus();
});

connect();
