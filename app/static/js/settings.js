const PROVIDER_PRESETS = {
    "ytclip":   { base_url: "https://ai-api.ytclip.org/v1",             label: "YTClip AI" },
    "openai":   { base_url: "https://api.openai.com/v1",                label: "OpenAI" },
    "groq":     { base_url: "https://api.groq.com/openai/v1",           label: "Groq" },
    "google":   { base_url: "https://generativelanguage.googleapis.com/v1beta", label: "Google Gemini" },
    "anthropic":{ base_url: "https://api.anthropic.com",                label: "Anthropic" },
    "custom":   { base_url: "",                                         label: "Custom" },
};

function authHeaders(extra) {
    return Object.assign({Authorization: "Bearer " + API_TOKEN, "Content-Type": "application/json"}, extra || {});
}

async function savePartial(body) {
    const res = await fetch("/api/settings", {
        method: "PUT", headers: authHeaders(),
        body: JSON.stringify(body),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        showToast(err.detail || "Failed to save", true);
        return;
    }
    showToast("Saved");
}

// ── Load ──
async function loadSettings() {
    const res = await fetch("/api/settings", {headers: authHeaders()});
    if (!res.ok) { showToast("Failed to load settings", true); return; }
    populateForm(await res.json());
}

// ── Populate ──
function populateForm(d) {
    for (const id of ["llm_timeout","llm_max_retries","stt_mode","stt_base_url","stt_model","stt_model_fallback","cookies_file","cookies_from_browser","concurrent_fragments","throttled_rate","aria2c_connections","public_base_url","repliz_default_account_id","repliz_post_type","repliz_schedule_offset_minutes","aws_endpoint","aws_bucket","aws_region","aws_folder_name"]) {
        const el = document.getElementById(id);
        if (el) el.value = d[id] ?? "";
    }
    for (const id of ["aria2c_enabled","repliz_auto_schedule","aws_use_path_style"]) {
        const el = document.getElementById(id);
        if (el) el.checked = !!d[id];
    }
    for (const id of ["api_token","stt_api_key","repliz_access_key","repliz_secret_key","aws_access_key_id","aws_secret_access_key"]) {
        const el = document.getElementById(id);
        if (el) el.value = d[id] ?? "";
    }
    toggleSttFields();

    const routers = d.llm_routers || [];
    renderLlmRouters(routers);

    const prov = d.ai_providers || {};
    for (const [task, p] of Object.entries(prov)) {
        const key = task.replace(/_/g, "-");
        const sel = document.getElementById(`provider-type-${key}`);
        if (sel) {
            const match = Object.keys(PROVIDER_PRESETS).find(k => PROVIDER_PRESETS[k].base_url === (p.base_url || "").replace(/\/+$/, ""));
            sel.value = match || "custom";
            onProviderTypeChange(key);
        }
        setVal(`provider-url-${key}`, p.base_url || "");
        const ci = document.getElementById(`provider-custom-url-input-${key}`);
        if (ci) ci.value = p.base_url || "";
        setVal(`provider-key-${key}`, p.api_key || "");
        setVal(`provider-model-${key}`, p.model || "");
    }

    const wm = d.watermark || {};
    setVal("wm-enabled", wm.enabled);
    setVal("wm-pos-x", (wm.pos_x ?? 0.85) * 100);
    setVal("wm-pos-y", (wm.pos_y ?? 0.05) * 100);
    setVal("wm-opacity", (wm.opacity ?? 0.8) * 100);
    setVal("wm-scale", (wm.scale ?? 0.12) * 100);
    setVal("wm-path", wm.image_path || "");
    updateWmPreview();
    drawWmCanvas();

    const cr = d.credit_watermark || {};
    setVal("cr-enabled", cr.enabled);
    setVal("cr-pos-x", (cr.pos_x ?? 0.5) * 100);
    setVal("cr-pos-y", (cr.pos_y ?? 0.95) * 100);
    setVal("cr-size", (cr.size ?? 0.022) * 100);
    setVal("cr-opacity", (cr.opacity ?? 0.3) * 100);
    drawCreditCanvas();

    const hk = d.hook_style || {};
    setVal("hk-font-size", (hk.font_size ?? 0.045) * 100);
    setVal("hk-font-color", hk.font_color || "#00a000");
    syncColorInput("hk-font-color");
    setVal("hk-bg-color", hk.bg_color || "#FFFFFF");
    syncColorInput("hk-bg-color");
    setVal("hk-corner-radius", hk.corner_radius ?? 8);
    document.getElementById("hk-corner-radius-val").textContent = hk.corner_radius ?? 8;
    setVal("hk-pos-x", (hk.pos_x ?? 0.5) * 100);
    setVal("hk-pos-y", (hk.pos_y ?? 0.7) * 100);
    drawHookCanvas();
}

function syncColorInput(id) {
    const picker = document.getElementById(id);
    const hex = document.getElementById(id + "-hex");
    if (picker && hex) hex.value = picker.value;
}

function setVal(id, val) {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!val;
    else el.value = val;
}

function getVal(id) {
    const el = document.getElementById(id);
    if (!el) return null;
    if (el.type === "checkbox") return el.checked;
    const n = parseFloat(el.value);
    if (el.type === "range" || el.type === "number") return isNaN(n) ? 0 : n;
    return el.value;
}

// ── LLM Routers ──
function renderLlmRouters(routers) {
    const container = document.getElementById("llm-routers-list");
    container.innerHTML = "";
    routers.forEach((r, i) => addLlmRouterRow(r, i));
    if (!routers.length) addLlmRouterRow({base_url: "", api_key: "", model: ""}, 0);
}

function addLlmRouterRow(r, i) {
    const container = document.getElementById("llm-routers-list");
    const row = document.createElement("div");
    row.className = "llm-router-row";
    row.innerHTML = `
        <div class="settings-field"><label>Base URL</label><input type="url" id="llm-url-${i}" value="${escHtml(r.base_url || "")}"></div>
        <div class="settings-field"><label>API Key</label><input type="text" id="llm-key-${i}" value="${escHtml(r.api_key || "")}"></div>
        <div class="settings-field"><label>Model</label><input type="text" id="llm-model-${i}" value="${escHtml(r.model || "")}" placeholder="e.g. gpt-4o-mini"></div>
        <div class="settings-field" style="align-self:end;"><button type="button" class="btn-danger-sm" onclick="removeLlmRouter(this)" ${i === 0 && container.children.length === 0 ? "" : ""}>&times;</button></div>
    `;
    container.appendChild(row);
}

function removeLlmRouter(btn) {
    const row = btn.closest(".llm-router-row");
    row.parentElement.removeChild(row);
}

function addLlmRouter() {
    const container = document.getElementById("llm-routers-list");
    addLlmRouterRow({base_url: "", api_key: "", model: ""}, container.children.length);
}

function escHtml(s) { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

function gatherLlmRouters() {
    const rows = document.querySelectorAll("#llm-routers-list .llm-router-row");
    return Array.from(rows).map(row => ({
        base_url: row.querySelector('[id^="llm-url-"]').value,
        api_key: row.querySelector('[id^="llm-key-"]').value,
        model: row.querySelector('[id^="llm-model-"]').value,
    }));
}

// ── AI Providers ──
function onProviderTypeChange(taskKey) {
    const sel = document.getElementById(`provider-type-${taskKey}`);
    const urlInput = document.getElementById(`provider-url-${taskKey}`);
    const customRow = document.getElementById(`provider-custom-url-${taskKey}`);
    const customInput = document.getElementById(`provider-custom-url-input-${taskKey}`);
    if (!sel || !urlInput) return;
    const preset = PROVIDER_PRESETS[sel.value];
    if (sel.value === "custom") {
        customRow.classList.remove("hidden");
        if (customInput) customInput.value = urlInput.value;
    } else {
        customRow.classList.add("hidden");
        if (preset) urlInput.value = preset.base_url;
    }
}

async function testProvider(taskKey) {
    const btn = document.getElementById(`provider-test-${taskKey}`);
    const statusEl = document.getElementById(`provider-status-${taskKey}`);
    btn.disabled = true;
    statusEl.textContent = "Testing...";
    statusEl.className = "provider-status";

    const sel = document.getElementById(`provider-type-${taskKey}`);
    const isCustom = sel && sel.value === "custom";
    const baseUrl = isCustom
        ? document.getElementById(`provider-custom-url-input-${taskKey}`).value
        : document.getElementById(`provider-url-${taskKey}`).value;
    const apiKey = document.getElementById(`provider-key-${taskKey}`).value;
    try {
        const res = await fetch("/api/settings/test-provider", {
            method: "POST", headers: authHeaders(),
            body: JSON.stringify({base_url: baseUrl, api_key: apiKey}),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Test failed");
        statusEl.textContent = `OK — ${(data.models || []).length} models`;
        statusEl.className = "provider-status ok";
        const modelSelect = document.getElementById(`provider-model-${taskKey}`);
        if (modelSelect && data.models && data.models.length) {
            const cur = modelSelect.value;
            modelSelect.innerHTML = '<option value="">Select model…</option>';
            for (const m of data.models) {
                const opt = document.createElement("option");
                opt.value = m; opt.textContent = m;
                if (m === cur) opt.selected = true;
                modelSelect.appendChild(opt);
            }
        }
    } catch (e) {
        statusEl.textContent = e.message || "Test failed";
        statusEl.className = "provider-status err";
    } finally { btn.disabled = false; }
}

// ── Watermark ──
function uploadWatermark() {
    const fi = document.getElementById("wm-upload");
    if (!fi || !fi.files || !fi.files[0]) { showToast("Select a PNG file first", true); return; }
    const fd = new FormData();
    fd.append("file", fi.files[0]);
    fetch("/api/settings/upload-watermark", {method:"POST", headers:{Authorization:"Bearer "+API_TOKEN}, body:fd})
    .then(r => r.json()).then(d => {
        if (d.path) { document.getElementById("wm-path").value = d.path; showToast("Watermark uploaded"); updateWmPreview(); drawWmCanvas(); }
        else showToast(d.detail || "Upload failed", true);
    }).catch(e => showToast(e.message, true));
}

function updateWmPreview() {
    const path = document.getElementById("wm-path").value;
    const img = document.getElementById("wm-preview-img");
    if (!img) return;
    if (path) { img.src = "/" + path; img.style.display = "block"; }
    else img.style.display = "none";
}

function onRange(id, valId) {
    const el = document.getElementById(id);
    const val = document.getElementById(valId);
    if (el && val) val.textContent = el.value + "%";
    if (id.startsWith("wm-")) drawWmCanvas();
    if (id.startsWith("cr-")) drawCreditCanvas();
    if (id.startsWith("hk-")) drawHookCanvas();
}

function drawWmCanvas() {
    const canvas = document.getElementById("wm-preview-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#222"; ctx.fillRect(0, 0, W, H);
    const img = document.getElementById("wm-preview-img");
    if (!img || img.style.display === "none") {
        ctx.fillStyle = "#555";
        ctx.font = "11px sans-serif"; ctx.textAlign = "center";
        ctx.fillText("Upload watermark PNG", W/2, H/2);
        return;
    }
    const px = parseFloat(getVal("wm-pos-x")) / 100 || 0.85;
    const py = parseFloat(getVal("wm-pos-y")) / 100 || 0.05;
    const op = parseFloat(getVal("wm-opacity")) / 100 || 0.8;
    const sc = parseFloat(getVal("wm-scale")) / 100 || 0.12;
    const iw = img.naturalWidth, ih = img.naturalHeight;
    if (!iw || !ih) return;
    const dw = W * sc, dh = dw * (ih / iw);
    const dx = W * px, dy = H * py;
    ctx.globalAlpha = op;
    ctx.drawImage(img, dx, dy, dw, dh);
    ctx.globalAlpha = 1;
}

function drawCreditCanvas() {
    const canvas = document.getElementById("cr-preview-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#222"; ctx.fillRect(0, 0, W, H);
    if (!document.getElementById("cr-enabled").checked) {
        ctx.fillStyle = "#555"; ctx.font = "11px sans-serif"; ctx.textAlign = "center";
        ctx.fillText("YT Source credit disabled", W/2, H/2);
        return;
    }
    const px = parseFloat(getVal("cr-pos-x")) / 100 || 0.5;
    const py = parseFloat(getVal("cr-pos-y")) / 100 || 0.95;
    const sz = parseFloat(getVal("cr-size")) / 100 || 0.022;
    const op = parseFloat(getVal("cr-opacity")) / 100 || 0.3;
    const fontSize = Math.max(8, H * sz);
    ctx.font = `${fontSize}px sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.globalAlpha = op;
    ctx.strokeStyle = "#000"; ctx.lineWidth = 2;
    ctx.strokeText("Source: Channel Name", W * px, H * py);
    ctx.fillStyle = "#fff";
    ctx.fillText("Source: Channel Name", W * px, H * py);
    ctx.globalAlpha = 1;
}

function drawHookCanvas() {
    const canvas = document.getElementById("hk-preview-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#222"; ctx.fillRect(0, 0, W, H);
    const fs = parseFloat(getVal("hk-font-size")) / 100 || 0.045;
    const fg = document.getElementById("hk-font-color").value || "#00a000";
    const bg = document.getElementById("hk-bg-color").value || "#fff";
    const cr = parseInt(getVal("hk-corner-radius")) || 8;
    const px = parseFloat(getVal("hk-pos-x")) / 100 || 0.5;
    const py = parseFloat(getVal("hk-pos-y")) / 100 || 0.7;
    const fontSize = Math.max(10, W * fs);
    ctx.font = `bold ${fontSize}px sans-serif`;
    const text = "HOOK TEXT";
    const tm = ctx.measureText(text);
    const pad = 8;
    const bx = W * px - tm.width / 2 - pad;
    const by = H * py - fontSize / 2 - pad;
    const bw = tm.width + pad * 2;
    const bh = fontSize + pad * 2;
    ctx.fillStyle = bg;
    roundRect(ctx, bx, by, bw, bh, Math.min(cr, bw/2, bh/2));
    ctx.fill();
    ctx.fillStyle = fg;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(text, W * px, H * py);
}

function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
}

// ── Section Save Functions ──

function saveAuth() {
    savePartial({ api_token: getVal("api_token") });
}

function saveAIProviders() {
    const ai_providers = {};
    for (const task of ["highlight-finder","caption-maker","hook-maker"]) {
        const key = task.replace(/-/g, "_");
        const sel = document.getElementById(`provider-type-${task}`);
        const isCustom = sel && sel.value === "custom";
        ai_providers[key] = {
            provider_type: sel ? sel.value : "openai",
            base_url: isCustom
                ? document.getElementById(`provider-custom-url-input-${task}`).value
                : document.getElementById(`provider-url-${task}`).value,
            api_key: document.getElementById(`provider-key-${task}`).value,
            model: document.getElementById(`provider-model-${task}`).value,
        };
    }
    savePartial({ ai_providers });
}

function saveLlmRouters() {
    savePartial({ llm_routers: gatherLlmRouters() });
}

function saveStt() {
    savePartial({
        stt_mode: getVal("stt_mode"),
        stt_base_url: getVal("stt_base_url"),
        stt_api_key: getVal("stt_api_key"),
        stt_model: getVal("stt_model"),
        stt_model_fallback: getVal("stt_model_fallback"),
    });
}

function saveVideoDownload() {
    savePartial({
        cookies_file: getVal("cookies_file"),
        cookies_from_browser: getVal("cookies_from_browser"),
        concurrent_fragments: getVal("concurrent_fragments"),
        throttled_rate: getVal("throttled_rate"),
        aria2c_connections: getVal("aria2c_connections"),
        aria2c_enabled: getVal("aria2c_enabled"),
    });
}

function saveRepliz() {
    savePartial({
        repliz_access_key: getVal("repliz_access_key"),
        repliz_secret_key: getVal("repliz_secret_key"),
        public_base_url: getVal("public_base_url"),
        repliz_default_account_id: getVal("repliz_default_account_id"),
        repliz_post_type: getVal("repliz_post_type"),
        repliz_schedule_offset_minutes: getVal("repliz_schedule_offset_minutes"),
        repliz_auto_schedule: getVal("repliz_auto_schedule"),
    });
}

function saveS3() {
    savePartial({
        aws_endpoint: getVal("aws_endpoint"),
        aws_bucket: getVal("aws_bucket"),
        aws_region: getVal("aws_region"),
        aws_folder_name: getVal("aws_folder_name"),
        aws_access_key_id: getVal("aws_access_key_id"),
        aws_secret_access_key: getVal("aws_secret_access_key"),
        aws_use_path_style: getVal("aws_use_path_style"),
    });
}

function saveWatermark() {
    savePartial({
        watermark: {
            enabled: getVal("wm-enabled"),
            image_path: getVal("wm-path"),
            pos_x: parseFloat(getVal("wm-pos-x")) / 100 || 0.85,
            pos_y: parseFloat(getVal("wm-pos-y")) / 100 || 0.05,
            opacity: parseFloat(getVal("wm-opacity")) / 100 || 0.8,
            scale: parseFloat(getVal("wm-scale")) / 100 || 0.12,
        },
    });
}

function saveCreditWatermark() {
    savePartial({
        credit_watermark: {
            enabled: getVal("cr-enabled"),
            pos_x: parseFloat(getVal("cr-pos-x")) / 100 || 0.5,
            pos_y: parseFloat(getVal("cr-pos-y")) / 100 || 0.95,
            size: parseFloat(getVal("cr-size")) / 100 || 0.022,
            opacity: parseFloat(getVal("cr-opacity")) / 100 || 0.3,
        },
    });
}

function saveHookStyle() {
    savePartial({
        hook_style: {
            font_size: parseFloat(getVal("hk-font-size")) / 100 || 0.045,
            font_color: getVal("hk-font-color"),
            bg_color: getVal("hk-bg-color"),
            corner_radius: parseInt(getVal("hk-corner-radius")) || 8,
            pos_x: parseFloat(getVal("hk-pos-x")) / 100 || 0.5,
            pos_y: parseFloat(getVal("hk-pos-y")) / 100 || 0.7,
        },
    });
}

function saveLlmAdvanced() {
    savePartial({
        llm_timeout: getVal("llm_timeout"),
        llm_max_retries: getVal("llm_max_retries"),
    });
}

// ── Toggle STT fields ──
function toggleSttFields() {
    const mode = document.getElementById("stt_mode");
    const fields = document.getElementById("stt-router-fields");
    if (mode && fields) fields.classList.toggle("visible", mode.value === "router");
}

document.addEventListener("DOMContentLoaded", function() {
    document.querySelectorAll('input:not([type="checkbox"]):not([type="range"]):not([type="color"]):not([type="file"]):not([type="hidden"])').forEach(function(el) {
        el.setAttribute("autocomplete", "off");
    });
    loadSettings();
});
