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

  // Forms that apply as soon as a choice changes: <form data-autosubmit> (the button stays
  // for keyboard users and without JavaScript).
  document.addEventListener("change", (e) => {
    const form = e.target.closest && e.target.closest("form[data-autosubmit]");
    if (form && e.target.matches("select")) form.requestSubmit();
  });

  // Confirm dialogs: <form data-confirm="Really?">. Then, for every form: the button that
  // sent it shows it's working, and a second click doesn't send it again (a second
  // reboot, a second VM). Forms that download a file (data-download) stay on the page, so
  // they're usable again after a moment. htmx forms handle their own requests.
  document.addEventListener("submit", (e) => {
    const form = e.target;
    const msg = form.dataset && form.dataset.confirm;
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    if (e.defaultPrevented || form.hasAttribute("hx-post") || form.hasAttribute("hx-get")) return;
    if (form.dataset.sent) { e.preventDefault(); return; }
    form.dataset.sent = "1";
    const button = e.submitter || form.querySelector("button:not([type=button]), input[type=submit]");
    if (button) {
      button.setAttribute("aria-busy", "true");
      button.classList.add("sending");
    }
    if (form.hasAttribute("data-download")) {
      setTimeout(() => {
        delete form.dataset.sent;
        if (button) { button.removeAttribute("aria-busy"); button.classList.remove("sending"); }
      }, 3000);
    }
  });
  // Back/forward cache: a page shown again must not keep its forms locked.
  window.addEventListener("pageshow", (e) => {
    if (!e.persisted) return;
    document.querySelectorAll("form[data-sent]").forEach((f) => delete f.dataset.sent);
    document.querySelectorAll(".sending").forEach((b) => { b.classList.remove("sending"); b.removeAttribute("aria-busy"); });
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

  // Escape closes an open menu and hands the focus back to its button.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    document.querySelectorAll("details.menu[open]").forEach((d) => {
      const hadFocus = d.contains(document.activeElement);
      d.open = false;
      if (hadFocus) d.querySelector("summary").focus();
    });
  });

  // <select data-details>: the line under it describes the chosen option (free space…).
  document.addEventListener("change", (e) => {
    const s = e.target.closest && e.target.closest("select[data-details]");
    const text = s && s.closest(".field") && s.closest(".field").querySelector("[data-detail-text]");
    if (text) text.textContent = (s.selectedOptions[0] && s.selectedOptions[0].dataset.detail) || "";
  });

  // Name -> id fields: <form data-slug-form> fills [data-slug-target] from [data-slug-source]
  // until the user edits a target themselves.
  document.querySelectorAll("[data-slug-form]").forEach((f) => {
    const src = f.querySelector("[data-slug-source]");
    const targets = [...f.querySelectorAll("[data-slug-target]")];
    if (!src) return;
    targets.forEach((t) => t.addEventListener("input", () => { t.dataset.touched = "1"; }));
    src.addEventListener("input", () => {
      const slug = src.value.toLowerCase().normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
        .replace(/[^a-z0-9.]+/g, "-").replace(/^[-.]+|[-.]+$/g, "").slice(0, 48);
      targets.forEach((t) => { if (!t.dataset.touched) t.value = slug; });
    });
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
      document.getElementById("job-cancel")?.remove();
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

    // A clone's disk can't be smaller than its template's: no smaller choices, and the
    // size moves up to it when a bigger template is picked.
    const applyMinDisk = (min) => {
      const input = $("#disk_gb"), hint = $("#disk-min-hint"), slider = $("[data-slider=disk_gb]");
      if (!input) return;
      const floor = +input.dataset.floor || 10;
      min = Math.max(min, floor);
      input.min = min;
      if (+input.value < min) input.value = min;
      slider?.querySelectorAll(".step").forEach((s) => {
        const r = s.querySelector("input"), off = +r.value < min;
        r.disabled = off;
        s.classList.toggle("off", off);
      });
      if (hint) {
        hint.textContent = `This template's disk is ${min} GB, so the VM's disk can't be smaller.`;
        hint.hidden = min <= floor;
      }
      if (slider) paintSlider(slider);
    };

    // Image rows: the version dropdown picks the template; the chosen image decides
    // the login user and which options apply (disabled fields aren't submitted).
    form.querySelectorAll("[data-image-choice]").forEach((row) => {
      const radio = row.querySelector("input[type=radio]");
      const select = row.querySelector("select");
      select.addEventListener("change", () => {
        radio.value = select.value;
        radio.dataset.minDisk = select.selectedOptions[0]?.dataset.minDisk || "";
        radio.checked = true; applyImage(); summary();
      });
      select.addEventListener("click", (e) => e.stopPropagation());
    });
    const applyImage = () => {
      const picked = $("input[name=template]:checked");
      const row = picked?.closest("[data-image-choice]");
      if (!row) return;
      applyMinDisk(+picked.dataset.minDisk || 0);
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
  }
})();
