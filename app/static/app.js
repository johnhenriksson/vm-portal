// All page behaviour lives here so the CSP can forbid inline scripts.
// Data comes from data-* attributes (HTML-escaped by Jinja), never from inline JS.
(() => {
  "use strict";

  // Clipboard. navigator.clipboard only exists on secure origins (HTTPS or
  // localhost); over plain HTTP fall back to copying from a temporary textarea.
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return true; } catch { /* fall back */ }
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    return ok;
  }

  // Briefly show a result on the element that was clicked.
  function flash(el, text) {
    if (!el.dataset.label) el.dataset.label = el.textContent;
    el.textContent = text;
    clearTimeout(el._flash);
    el._flash = setTimeout(() => { el.textContent = el.dataset.label; }, 2000);
  }

  // If copying failed, select the text in a nearby field so Ctrl+C works.
  function selectFallback(el) {
    const input = el.closest(".row, .card-body")?.querySelector("input[readonly]");
    if (input) { input.focus(); input.select(); }
  }

  // Confirm dialogs: <form data-confirm="Really?">
  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  document.addEventListener("click", (e) => {
    // Copy to clipboard: <button data-copy="text">
    const copy = e.target.closest("[data-copy]");
    if (copy) {
      e.preventDefault();
      copyText(copy.dataset.copy).then((ok) => {
        flash(copy, ok ? "Copied" : "Press Ctrl+C");
        if (!ok) selectFallback(copy);
        const menu = copy.closest("details");
        if (menu && ok) menu.open = false;
      });
      return;
    }
    // Fill the static IP field: <button data-fill-ip="10.0.0.5">
    const fill = e.target.closest("[data-fill-ip]");
    if (fill) {
      const input = document.getElementById("ip");
      input.value = fill.dataset.fillIp;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }
    // Close open "More" menus when clicking elsewhere.
    document.querySelectorAll("details.menu[open]").forEach((d) => {
      if (!d.contains(e.target)) d.open = false;
    });
  });

  // Code editor: Tab inserts two spaces instead of leaving the textarea.
  document.addEventListener("keydown", (e) => {
    const ta = e.target;
    if (e.key !== "Tab" || !ta.matches("textarea.code") || ta.readOnly || e.shiftKey) return;
    e.preventDefault();
    ta.setRangeText("  ", ta.selectionStart, ta.selectionEnd, "end");
  });

  // Select-on-click for read-only inputs.
  document.addEventListener("focusin", (e) => {
    if (e.target.matches("input[readonly]")) e.target.select();
  });

  // Job log streaming: <pre id="log" data-stream="/jobs/1/stream">
  const log = document.getElementById("log");
  if (log && log.dataset.stream) {
    const follow = document.getElementById("follow");
    const es = new EventSource(log.dataset.stream);
    es.onmessage = (e) => {
      log.textContent += e.data + "\n";
      if (!follow || follow.checked) log.scrollTop = log.scrollHeight;
    };
    es.addEventListener("done", (e) => {
      es.close();
      const s = document.createElement("span");
      s.className = "badge " + e.data.replace(/[^a-z]/g, "");
      s.textContent = e.data;
      document.getElementById("job-status").replaceChildren(s);
    });
  }

  // Create-VM form: stepped sliders, network tabs, live summary, step bar.
  const form = document.getElementById("vm-form");
  if (form) {
    const $ = (s) => form.querySelector(s);
    const fmt = {
      cpus: (v) => `${v} vCPU${v == 1 ? "" : "s"}`,
      memory_mb: (v) => `${+(v / 1024).toFixed(1)} GB RAM`,
      disk_gb: (v) => `${v} GB disk`,
    };

    const paintSlider = (el) => {
      const name = el.dataset.slider, input = document.getElementById(name);
      const steps = [...el.querySelectorAll(".step")];
      const radios = steps.map((s) => s.querySelector("input"));
      const idx = radios.findIndex((r) => r.value === input.value);
      radios.forEach((r, i) => (r.checked = i === idx));
      steps.forEach((s, i) => s.classList.toggle("on", idx >= 0 && i < idx));
      el.querySelector(".fill").style.width = idx > 0 ? `calc((100% - 28px) * ${idx / (steps.length - 1)})` : "0";
      el.querySelector("[data-value]").textContent = fmt[name](input.value);
    };

    const summary = () => {
      const set = (k, v) => ($(`[data-sum=${k}]`).textContent = v || "–");
      set("name", $("#name").value);
      set("template", $("input[name=template]:checked")?.value);
      set("size", `${$("#cpus").value} vCPU / ${+($("#memory_mb").value / 1024).toFixed(1)} GB / ${$("#disk_gb").value} GB`);
      const stat = $("input[name=ip_mode]:checked").value === "static";
      set("net", stat ? ($("#ip").value || "Static (not set)") : "DHCP");
      set("console", $("#console_password") && !$("#console_password").disabled && $("#console_password").value ? "Password" : "Off");
      $("#static-pane").hidden = !stat;
      $("#dhcp-pane").hidden = stat;
    };

    // Image rows: the version dropdown picks the template; the chosen image decides
    // the login user and which options apply (disabled fields aren't submitted).
    form.querySelectorAll("[data-image-choice]").forEach((row) => {
      const radio = row.querySelector("input[type=radio]");
      const select = row.querySelector(".version-select");
      select.addEventListener("change", () => { radio.value = select.value; radio.checked = true; applyImage(); summary(); });
      select.addEventListener("click", (e) => e.stopPropagation());
    });
    const applyImage = () => {
      const row = $("input[name=template]:checked")?.closest("[data-image-choice]");
      if (!row) return;
      form.querySelectorAll("[data-show-login-user]").forEach((el) => (el.textContent = row.dataset.loginUser));
      for (const [need, attr] of [["docker", "docker"], ["console-password", "consolePassword"]]) {
        const on = row.dataset[attr] === "yes";
        form.querySelectorAll(`[data-needs=${need}]`).forEach((box) => {
          box.hidden = !on;
          box.querySelectorAll("input, textarea").forEach((i) => (i.disabled = !on));
        });
      }
    };
    form.addEventListener("change", (e) => { if (e.target.name === "template") applyImage(); });
    applyImage();

    form.querySelectorAll("[data-slider]").forEach((el) => {
      const input = document.getElementById(el.dataset.slider);
      el.addEventListener("change", (e) => { input.value = e.target.value; paintSlider(el); summary(); });
      input.addEventListener("input", () => { paintSlider(el); summary(); });
      paintSlider(el);
    });
    form.addEventListener("input", summary);
    form.addEventListener("change", summary);
    document.body.addEventListener("htmx:afterSwap", summary);
    summary();

    const links = [...document.querySelectorAll("#stepbar .s")];
    const obs = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (en.isIntersecting) links.forEach((a) => a.classList.toggle("active", a.hash === "#" + en.target.id));
      });
    }, { rootMargin: "-20% 0px -70% 0px" });
    form.querySelectorAll(".create-section").forEach((s) => obs.observe(s));
  }
})();
