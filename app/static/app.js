"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const MAX_UPLOAD_MB = Number(document.body.dataset.maxUploadMb || 200);

  const state = {
    mode: "upload",
    environments: [],
    eventSource: null,
    deploying: false,
  };

  // ---------- helpers ----------

  function formatDetail(detail) {
    if (!detail) return "";
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
    }
    return JSON.stringify(detail);
  }

  async function api(url, { method = "GET", json, body } = {}) {
    const headers = { "X-CSRF-Token": csrf, Accept: "application/json" };
    if (json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(json);
    }
    const res = await fetch(url, { method, headers, body, credentials: "same-origin" });
    if (res.status === 401) {
      window.location.href = "/login";
      throw new Error("Session expired.");
    }
    const isJson = (res.headers.get("content-type") || "").includes("application/json");
    const data = isJson ? await res.json() : null;
    if (!res.ok) throw new Error(formatDetail(data && data.detail) || `HTTP ${res.status}`);
    return data;
  }

  function deployBranchName() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `deploy/eb-update-${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}-` +
      `${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}`;
  }

  function setOptions(select, items, placeholder) {
    select.replaceChildren();
    const ph = document.createElement("option");
    ph.value = "";
    ph.textContent = placeholder;
    select.appendChild(ph);
    for (const { value, label } of items) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      select.appendChild(opt);
    }
  }

  function setDatalist(list, values) {
    list.replaceChildren(...values.map((v) => Object.assign(document.createElement("option"), { value: v })));
  }

  async function withBusy(button, fn) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Working…";
    try {
      return await fn();
    } catch (err) {
      log(err.message, "error", "ui");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  // ---------- console ----------

  const consoleEl = $("console");
  const LEVEL_CLASS = {
    info: "text-slate-300",
    warn: "text-amber-300",
    error: "text-red-400",
    success: "text-emerald-400 font-semibold",
  };
  const SOURCE_CLASS = { cmt: "text-indigo-400", eb: "text-cyan-400", ui: "text-fuchsia-400", gh: "text-emerald-300" };

  function log(message, level = "info", source = "ui", time = new Date().toISOString()) {
    const atBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 40;
    const line = document.createElement("div");
    line.className = "whitespace-pre-wrap break-words";
    const ts = document.createElement("span");
    ts.className = "text-slate-600";
    ts.textContent = new Date(time).toLocaleTimeString() + " ";
    const src = document.createElement("span");
    src.className = SOURCE_CLASS[source] || "text-slate-400";
    src.textContent = `[${source}] `;
    const msg = document.createElement("span");
    msg.className = LEVEL_CLASS[level] || LEVEL_CLASS.info;
    msg.textContent = message;
    line.append(ts, src, msg);
    consoleEl.appendChild(line);
    if (atBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
  }

  const STATUS_CLASS = {
    idle: "bg-slate-800 text-slate-300",
    running: "bg-indigo-500/20 text-indigo-300 animate-pulse",
    succeeded: "bg-emerald-500/20 text-emerald-300",
    failed: "bg-red-500/20 text-red-300",
  };

  function setDeployStatus(status) {
    const el = $("deploy-status");
    el.textContent = status;
    el.className = `rounded-full px-2.5 py-0.5 text-xs font-semibold ${STATUS_CLASS[status] || STATUS_CLASS.idle}`;
  }

  $("clear-console").addEventListener("click", () => consoleEl.replaceChildren());

  // ---------- AWS target ----------

  const accountSel = $("account");
  const envSel = $("environment");
  const HEALTH_DOT = { Green: "bg-emerald-500", Yellow: "bg-amber-400", Red: "bg-red-500", Grey: "bg-slate-500" };

  function renderEnvDetails() {
    const env = state.environments.find((e) => e.name === envSel.value);
    $("env-details").classList.toggle("hidden", !env);
    if (!env) return;
    $("env-health-dot").className = `h-2.5 w-2.5 rounded-full ${HEALTH_DOT[env.health] || HEALTH_DOT.Grey}`;
    $("env-health").textContent = env.health + (env.health_status ? ` (${env.health_status})` : "");
    $("env-status").textContent = env.status;
    $("env-app").textContent = env.application;
    $("env-version").textContent = env.version_label || "—";
    $("env-platform").textContent = env.platform || "—";
    $("env-cname").textContent = env.cname || "—";
    $("env-updated").textContent = env.updated ? new Date(env.updated).toLocaleString() : "—";
  }

  async function loadEnvironments() {
    const account = accountSel.value;
    const previous = envSel.value;
    state.environments = [];
    renderEnvDetails();
    if (!account) {
      envSel.disabled = true;
      setOptions(envSel, [], "Select an account first");
      return;
    }
    envSel.disabled = true;
    setOptions(envSel, [], "Loading environments…");
    try {
      state.environments = await api(`/api/aws/accounts/${encodeURIComponent(account)}/environments`);
      setOptions(
        envSel,
        state.environments.map((e) => ({ value: e.name, label: `${e.name} · ${e.application} · ${e.status}` })),
        state.environments.length ? "Select environment…" : "No environments found",
      );
      envSel.disabled = !state.environments.length;
      if (state.environments.some((e) => e.name === previous)) envSel.value = previous;
      renderEnvDetails();
    } catch (err) {
      setOptions(envSel, [], "Failed to load environments");
      log(`Loading environments failed: ${err.message}`, "error");
    }
  }

  accountSel.addEventListener("change", loadEnvironments);
  envSel.addEventListener("change", renderEnvDetails);
  $("env-refresh").addEventListener("click", loadEnvironments);

  // ---------- source tabs ----------

  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      const active = btn.dataset.tab === mode;
      btn.setAttribute("aria-selected", String(active));
      btn.className = `tab-btn rounded-md px-3 py-1.5 text-sm font-medium ${
        active ? "bg-indigo-600 text-white" : "text-slate-400 hover:text-slate-200"}`;
    });
    $("panel-upload").classList.toggle("hidden", mode !== "upload");
    $("panel-github").classList.toggle("hidden", mode !== "github");
    if (mode === "github") loadRepos();
  }
  document.querySelectorAll(".tab-btn").forEach((btn) => btn.addEventListener("click", () => setMode(btn.dataset.tab)));

  // ---------- Mode A: upload ----------

  const bundleInput = $("bundle");

  function validateBundle() {
    const file = bundleInput.files[0];
    const info = $("bundle-info");
    info.className = "text-sm";
    if (!file) {
      info.textContent = "";
      return null;
    }
    const sizeMb = file.size / 1048576;
    if (!file.name.toLowerCase().endsWith(".zip")) {
      info.textContent = `"${file.name}" is not a .zip file.`;
      info.classList.add("text-red-400");
      return null;
    }
    if (file.size === 0 || sizeMb > MAX_UPLOAD_MB) {
      info.textContent = file.size === 0 ? "File is empty." : `File is ${sizeMb.toFixed(1)} MB; limit is ${MAX_UPLOAD_MB} MB.`;
      info.classList.add("text-red-400");
      return null;
    }
    info.textContent = `${file.name} · ${sizeMb.toFixed(2)} MB`;
    info.classList.add("text-emerald-400");
    return file;
  }
  bundleInput.addEventListener("change", validateBundle);

  // ---------- Mode B: GitHub ----------

  const ghStatus = $("gh-status");
  let reposLoaded = false;

  function renderGitHubStatus(connected, login, mock) {
    ghStatus.textContent = connected
      ? `Connected as @${login}`
      : mock ? "No PAT · using simulated GitHub (dry-run)" : "Not connected · add a PAT to continue";
    ghStatus.className = connected ? "text-emerald-300" : mock ? "text-amber-300" : "text-red-300";
    $("gh-disconnect").classList.toggle("hidden", !connected);
    $("gh-token-form").classList.toggle("hidden", connected);
  }

  renderGitHubStatus(Boolean(ghStatus.dataset.login), ghStatus.dataset.login, ghStatus.dataset.mock === "true");

  $("gh-token-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const tokenInput = $("gh-token");
    const button = e.submitter || e.target.querySelector("button");
    await withBusy(button, async () => {
      const res = await api("/api/github/token", { method: "POST", json: { token: tokenInput.value.trim() } });
      tokenInput.value = "";
      renderGitHubStatus(true, res.login, false);
      log(`GitHub token verified for @${res.login} (scopes: ${res.scopes}).`, "success", "gh");
      reposLoaded = false;
      loadRepos();
    });
  });

  $("gh-disconnect").addEventListener("click", async () => {
    try {
      const status = await api("/api/github/token", { method: "DELETE" }).then(() => api("/api/github/status"));
      renderGitHubStatus(false, null, status.mock);
      log("GitHub token removed from session.", "info", "gh");
    } catch (err) {
      log(err.message, "error", "gh");
    }
  });

  async function loadRepos() {
    if (reposLoaded) return;
    try {
      const repos = await api("/api/github/repos");
      setDatalist($("repo-list"), repos.map((r) => r.full_name));
      reposLoaded = true;
    } catch (err) {
      // Repo suggestions are optional; a manual owner/repo still works.
      log(`Could not list repositories: ${err.message}`, "warn", "gh");
    }
  }

  const repoInput = $("repo");
  const baseSel = $("base-branch");
  const newBranchInput = $("new-branch");
  const deployRefInput = $("deploy-ref");
  newBranchInput.value = deployBranchName();
  $("regen-branch").addEventListener("click", () => { newBranchInput.value = deployBranchName(); });

  function requireRepo() {
    const repo = repoInput.value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/.test(repo)) {
      throw new Error("Enter the repository as owner/repo.");
    }
    return repo;
  }

  $("load-branches").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    const repo = requireRepo();
    const res = await api(`/api/github/branches?repo=${encodeURIComponent(repo)}`);
    setOptions(baseSel, res.branches.map((b) => ({ value: b, label: b })), "Select base branch…");
    baseSel.disabled = false;
    baseSel.value = res.branches.includes(res.repo.default_branch) ? res.repo.default_branch : "";
    setDatalist($("branch-list"), res.branches);
    if (!deployRefInput.value) deployRefInput.value = baseSel.value;
    log(`Loaded ${res.branches.length} branches for ${res.repo.full_name} (default: ${res.repo.default_branch}).`, "info", "gh");
  }));

  $("create-branch").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    const repo = requireRepo();
    if (!baseSel.value) throw new Error("Select a base branch.");
    const res = await api("/api/github/branches", {
      method: "POST",
      json: { repo, base_branch: baseSel.value, new_branch: newBranchInput.value.trim() || null },
    });
    deployRefInput.value = res.name;
    const opt = document.createElement("option");
    opt.value = res.name;
    $("branch-list").appendChild(opt);
    log(`Created branch ${res.name} from ${baseSel.value} @ ${res.sha.slice(0, 7)}.`, "success", "gh");
  }));

  function selectedConfigFiles() {
    return [...document.querySelectorAll('input[name="config-file"]:checked')].map((c) => c.value);
  }

  $("preview-config").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    const files = await api(`/api/github/sample-config?procfile_command=${encodeURIComponent($("procfile-cmd").value)}`);
    for (const path of selectedConfigFiles()) {
      log(`--- ${path} ---\n${files[path]}`, "info", "gh");
    }
  }));

  $("commit-config").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    const repo = requireRepo();
    const branch = deployRefInput.value.trim() || newBranchInput.value.trim();
    if (!branch) throw new Error("Create or enter a deployment branch first.");
    const files = selectedConfigFiles();
    if (!files.length) throw new Error("Select at least one configuration file.");
    if (["main", "master"].includes(branch) && !confirm(`Commit directly to '${branch}'?`)) return;
    const res = await api("/api/github/commit-config", {
      method: "POST",
      json: { repo, branch, files, procfile_command: $("procfile-cmd").value.trim() },
    });
    log(`Committed ${res.files.join(", ")} to ${branch}: ${res.sha.slice(0, 7)} ${res.html_url}`, "success", "gh");
  }));

  // ---------- Deploy + SSE ----------

  const deployBtn = $("deploy-btn");

  function finishDeploy(status) {
    state.deploying = false;
    deployBtn.disabled = false;
    setDeployStatus(status);
    loadEnvironments();
  }

  function streamDeployment(id) {
    if (state.eventSource) state.eventSource.close();
    const es = new EventSource(`/api/aws/deployments/${encodeURIComponent(id)}/events`);
    state.eventSource = es;

    es.addEventListener("log", (e) => {
      const entry = JSON.parse(e.data);
      log(entry.message, entry.level, entry.source, entry.time);
    });
    es.addEventListener("done", (e) => {
      const summary = JSON.parse(e.data);
      es.close();
      state.eventSource = null;
      finishDeploy(summary.status);
    });
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) {
        log("Event stream closed unexpectedly.", "error");
        finishDeploy("failed");
      } else {
        log("Event stream interrupted; reconnecting…", "warn");
      }
    };
  }

  deployBtn.addEventListener("click", async () => {
    if (state.deploying) return;
    try {
      const account = accountSel.value;
      const env = state.environments.find((x) => x.name === envSel.value);
      if (!account || !env) throw new Error("Select an AWS account and environment.");
      if (env.status !== "Ready") throw new Error(`Environment is ${env.status}; wait until it is Ready.`);

      const form = new FormData();
      form.append("account", account);
      form.append("environment", env.name);
      form.append("source", state.mode);
      let sourceLabel;
      if (state.mode === "upload") {
        const file = validateBundle();
        if (!file) throw new Error("Choose a valid .zip bundle.");
        form.append("bundle", file);
        sourceLabel = file.name;
      } else {
        const repo = requireRepo();
        const ref = deployRefInput.value.trim();
        if (!ref) throw new Error("Enter the branch to deploy.");
        form.append("repo", repo);
        form.append("ref", ref);
        sourceLabel = `${repo}@${ref}`;
      }

      const accountName = accountSel.selectedOptions[0].textContent;
      if (/prod/i.test(account) && !confirm(`Deploy ${sourceLabel} to PRODUCTION environment ${env.name}?`)) return;

      state.deploying = true;
      deployBtn.disabled = true;
      setDeployStatus("running");
      log(`Submitting ${sourceLabel} → ${env.name} (${accountName})…`, "info", "ui");
      const res = await api("/api/aws/deploy", { method: "POST", body: form });
      streamDeployment(res.deployment_id);
    } catch (err) {
      log(err.message, "error", "ui");
      if (state.deploying) finishDeploy("failed");
    }
  });

  // ---------- init ----------

  setMode("upload");
  setDeployStatus("idle");
  log("Console ready. Select a target and source, then deploy.", "info", "ui");
})();
