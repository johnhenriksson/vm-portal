// Code editor for image definition files: a plain <textarea> (so the form,
// undo and accessibility keep working) with line numbers and syntax colouring
// drawn behind it. No third-party code, no inline scripts (see the CSP).
(() => {
  "use strict";
  const box = document.querySelector(".code-editor");
  if (!box) return;
  const ta = box.querySelector("textarea");
  const form = ta.form;
  const stateEl = document.querySelector("[data-editor-state]");
  const posEl = document.querySelector("[data-editor-pos]");
  const modeEl = document.querySelector("[data-editor-mode]");
  const editable = !ta.readOnly;
  const INDENT = "  ";

  // --- languages -----------------------------------------------------------------
  // Each rule: [token class, regex source without capture groups]. Matched per line.
  const STR = String.raw`"(?:[^"\\]|\\.)*"?`;
  const SQ = String.raw`'[^']*'?`;
  const HASH_COMMENT = String.raw`(?<=^|\s)#.*`;
  const INTERP = String.raw`[$%]\{[^}]*\}?`;
  const LANGS = {
    hcl: { label: "HCL", rules: [
      ["c", String.raw`#.*|\/\/.*|\/\*.*?\*\/`], ["s", STR],
      ["k", String.raw`\b(?:packer|variable|locals?|source|build|provisioner|post-processors?|data|dynamic|required_plugins|validation|for|in|if|else|endif|endfor)\b`],
      ["n", String.raw`\b(?:true|false|null)\b|\b\d+(?:\.\d+)?\b`],
      ["a", String.raw`[A-Za-z_][\w-]*(?=\s*=(?!=))`],
    ] },
    yaml: { label: "YAML", rules: [
      ["c", HASH_COMMENT], ["s", `${STR}|${SQ}`],
      ["a", String.raw`^\s*(?:-\s+)?[\w./-]+(?=:(?:\s|$))`],
      ["n", String.raw`(?<=:\s+|-\s+)(?:true|false|null|yes|no|~)\s*$|(?<=:\s+|-\s+)-?\d+(?:\.\d+)?\s*$`],
      ["k", String.raw`^(?:---|\.\.\.)\s*$|^#cloud-config`],
    ] },
    shell: { label: "Shell", rules: [
      ["c", HASH_COMMENT], ["s", `${STR}|${SQ}`], ["v", String.raw`\$\{[^}]*\}|\$\w+|\$[@*#?$!0-9]`],
      ["k", String.raw`^%\w+|\b(?:if|then|else|elif|fi|for|while|until|do|done|case|esac|function|in|set|export|local|return|exit|sudo|echo|d-i)\b`],
    ] },
    ini: { label: "INI", rules: [
      ["c", String.raw`^\s*[#;].*`], ["k", String.raw`^\s*\[[^\]]*\]`], ["a", String.raw`^\s*[\w.-]+(?=\s*=)`],
    ] },
    powershell: { label: "PowerShell", rules: [
      ["c", HASH_COMMENT], ["s", `${STR}|${SQ}`], ["v", String.raw`\$[\w:]+`],
      ["k", String.raw`\b[A-Z][a-z]+-[A-Z]\w+\b|\b(?:if|else|elseif|foreach|function|param|return|try|catch|finally|throw)\b`],
    ] },
    json: { label: "JSON", rules: [
      ["a", String.raw`"(?:[^"\\]|\\.)*"(?=\s*:)`], ["s", STR], ["n", String.raw`\b(?:true|false|null)\b|-?\b\d+(?:\.\d+)?\b`],
    ] },
    xml: { label: "XML", rules: [
      ["c", String.raw`<!--.*?(?:-->|$)`], ["k", String.raw`<\/?[\w:.-]+|\/?>`], ["a", String.raw`[\w:.-]+(?==)`], ["s", `${STR}|${SQ}`],
    ] },
    text: { label: "Text", rules: [] },
  };
  const BY_NAME = { "user-data": "yaml", "meta-data": "yaml", "vendor-data": "yaml", "network-config": "yaml",
                    "ks": "shell", "preseed": "shell" };
  const BY_EXT = { hcl: "hcl", json: "json", yaml: "yaml", yml: "yaml", sh: "shell", ks: "shell", cfg: "shell",
                   seed: "shell", preseed: "shell", service: "ini", conf: "ini", inf: "ini", ps1: "powershell",
                   psm1: "powershell", xml: "xml" };

  function languageFor(path) {
    const name = path.split("/").pop();
    const template = /\.pkrtpl(\.hcl)?$/.test(name);
    const base = template ? name.replace(/\.pkrtpl(\.hcl)?$/, "") : name;
    const ext = base.includes(".") ? base.split(".").pop().toLowerCase() : "";
    const key = BY_NAME[base.split(".")[0]] || BY_EXT[ext] || (template ? "shell" : "text");
    const lang = LANGS[key];
    // Packer templates: ${...} and %{...} are interpolations whatever the language.
    const rules = template ? [["i", INTERP], ...lang.rules] : lang.rules;
    return {
      label: lang.label + (template ? " · Packer template" : ""),
      classes: rules.map(([cls]) => cls),
      re: rules.length ? new RegExp(rules.map(([, src]) => `(${src})`).join("|"), "g") : null,
    };
  }

  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  function highlightLine(line, lang) {
    if (!lang.re || line.length > 2000) return esc(line);
    let out = "", last = 0;
    for (const m of line.matchAll(lang.re)) {
      if (!m[0]) continue;
      const cls = lang.classes[m.slice(1).findIndex((g) => g !== undefined)];
      out += esc(line.slice(last, m.index)) + `<span class="t-${cls}">${esc(m[0])}</span>`;
      last = m.index + m[0].length;
    }
    return out + esc(line.slice(last));
  }

  // --- layout: gutter | (highlight layer under the textarea) ---------------------
  const lang = languageFor(box.dataset.path || "");
  const gutter = document.createElement("div");
  gutter.className = "gutter";
  gutter.setAttribute("aria-hidden", "true");
  const numbers = document.createElement("div");
  gutter.append(numbers);
  const area = document.createElement("div");
  area.className = "code-area";
  const layer = document.createElement("pre");
  layer.className = "hl";
  layer.setAttribute("aria-hidden", "true");
  const code = document.createElement("code");
  layer.append(code);
  ta.replaceWith(area);
  area.append(layer, ta);
  box.prepend(gutter);
  box.classList.add("enhanced");
  if (modeEl) modeEl.textContent = lang.label;

  let lineCount = 0, frame = 0;
  function render() {
    frame = 0;
    const lines = ta.value.split("\n");
    code.innerHTML = lines.map((l) => highlightLine(l, lang)).join("\n") + "\n";
    if (lines.length !== lineCount) {
      lineCount = lines.length;
      numbers.textContent = Array.from({ length: lineCount }, (_, i) => i + 1).join("\n");
    }
    sync();
  }
  const schedule = () => { if (!frame) frame = requestAnimationFrame(render); };
  function sync() {
    code.style.transform = `translate(${-ta.scrollLeft}px, ${-ta.scrollTop}px)`;
    numbers.style.transform = `translateY(${-ta.scrollTop}px)`;
  }
  ta.addEventListener("scroll", sync);
  ta.addEventListener("input", schedule);
  render();

  // --- state: unsaved changes, cursor position -----------------------------------
  const original = ta.value;
  let submitting = false;
  const dirty = () => editable && ta.value !== original;
  function updateState() {
    if (!stateEl || !editable) return;
    stateEl.textContent = dirty() ? "Unsaved changes" : "Saved";
    stateEl.classList.toggle("dirty", dirty());
  }
  function updatePos() {
    if (!posEl) return;
    const before = ta.value.slice(0, ta.selectionStart);
    const line = before.split("\n").length;
    const col = ta.selectionStart - before.lastIndexOf("\n");
    const sel = Math.abs(ta.selectionEnd - ta.selectionStart);
    posEl.textContent = `Ln ${line}, Col ${col}` + (sel ? ` (${sel} selected)` : "");
  }
  ta.addEventListener("input", updateState);
  document.addEventListener("selectionchange", () => { if (document.activeElement === ta) updatePos(); });
  ta.addEventListener("focus", updatePos);
  updatePos();

  window.addEventListener("beforeunload", (e) => {
    if (dirty() && !submitting) e.preventDefault();
  });
  form.addEventListener("submit", (e) => {
    if (e.submitter && e.submitter.hasAttribute("data-validate") && dirty() &&
        !window.confirm("Validate checks the saved files. Discard your unsaved changes?")) {
      e.preventDefault();
      return;
    }
    submitting = true;
  });

  // --- keys ------------------------------------------------------------------------
  // Insert through execCommand where possible so the browser's undo history keeps working.
  function insert(text) {
    if (!document.execCommand("insertText", false, text)) {
      ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, "end");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }
  function lineStart(pos) { return ta.value.lastIndexOf("\n", pos - 1) + 1; }

  function indentLines(outdent) {
    const start = lineStart(ta.selectionStart);
    let end = ta.selectionEnd;
    if (end > start && ta.value[end - 1] === "\n") end -= 1;  // selection ending at a line start
    const block = ta.value.slice(start, end);
    const changed = block.split("\n").map((l) => (outdent ? l.replace(/^ {1,2}|^\t/, "") : INDENT + l)).join("\n");
    ta.setSelectionRange(start, end);
    insert(changed);
    ta.setSelectionRange(start, start + changed.length);
  }

  let escaped = false;  // Esc, then Tab, moves focus on instead of indenting (keyboard users)
  ta.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (editable) { submitting = true; form.requestSubmit(); }
      return;
    }
    if (!editable) return;
    if (e.key === "Escape") { escaped = true; return; }
    if (e.key === "Tab") {
      if (escaped) { escaped = false; return; }
      e.preventDefault();
      const multi = ta.value.slice(ta.selectionStart, ta.selectionEnd).includes("\n");
      if (e.shiftKey || multi) indentLines(e.shiftKey);
      else insert(INDENT);
      return;
    }
    escaped = false;
    if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey && !e.isComposing) {
      e.preventDefault();
      const start = lineStart(ta.selectionStart);
      const current = ta.value.slice(start, ta.selectionStart);
      let indent = current.match(/^[ \t]*/)[0];
      if (/[{[(]\s*$/.test(current) || (lang.label.startsWith("YAML") && /:\s*$/.test(current))) indent += INDENT;
      insert("\n" + indent);
    }
  });
})();
